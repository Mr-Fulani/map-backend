from contextlib import nullcontext
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry
from django.utils import timezone

from apps.core.models import BackgroundJobDispatch
from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.marketplaces.adapters.avito.adapter import (
    AmbiguousFeedSubmissionError,
    FeedUploadError,
)
from apps.marketplaces.models import MarketplaceAccount
from apps.marketplaces.models import Listing
from apps.marketplaces.adapters.avito.rate_limiter import RateLimitError
from apps.marketplaces.tasks import (
    _CACHE_UNAVAILABLE,
    _cache_clear_feed_flush_owner,
    _cache_get_feed_flush_owner,
    _cache_refresh_feed_flush_owner,
    coalesced_flush_task,
    dispatch_due_marketplace_feed_intents,
    request_feed_flush,
)
from apps.tenants.models import Tenant
from apps.products.models import Product


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _legacy_feed_modes(settings):
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'legacy'
    settings.MARKETPLACE_FEED_RUN_MODE = 'legacy'


def _account(
    suffix: str,
    *,
    revision: int = 0,
    dispatched_revision: int = 0,
    due_at=None,
) -> MarketplaceAccount:
    tenant = Tenant.objects.create(
        name=f'Feed repair {suffix}',
        slug=f'feed-repair-{suffix}'[:50],
    )
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Feed repair {suffix}',
        external_id=f'feed-repair-{suffix}',
        credentials_enc=b'opaque-test-credentials',
        feed_intent_revision=revision,
        feed_intent_dispatched_revision=dispatched_revision,
        feed_intent_due_at=due_at,
    )


def _advisory_lock(_identity: str):
    return nullcontext(True)


def _pending_listings(
    account: MarketplaceAccount,
    *,
    count: int,
) -> list[Listing]:
    datasource = DataSourceConnection.objects.create(
        tenant=account.tenant,
        name=f'Feed repair source {account.pk}',
        type='1c_http',
        credentials=encrypt({
            'url': 'http://source.invalid',
            'user': 'test',
            'password': 'test',
        }),
    )
    listings = []
    for index in range(count):
        product = Product.objects.create(
            tenant=account.tenant,
            datasource=datasource,
            article=f'REPAIR-{account.pk}-{index}',
            name=f'Feed repair product {index}',
            brand='Bosch',
            price='1000',
            stock_qty=1,
            category_1c='Запчасти',
            condition='new',
        )
        listings.append(Listing.objects.create(
            tenant=account.tenant,
            product=product,
            account=account,
            status=Listing.STATUS_PENDING,
            price_on_listing=Decimal('1000'),
            title=f'Feed repair listing {index}',
            description_ai='Feed repair test listing.',
        ))
    return listings


def test_legacy_request_commits_desired_cursor_before_broker_publish_failure():
    account = _account('legacy-publish-failure')

    with (
        patch(
            'apps.marketplaces.tasks._cache_add_feed_flush_owner',
            return_value=None,
        ),
        patch(
            'apps.marketplaces.tasks._cache_clear_feed_flush_owner',
        ),
        patch(
            'apps.marketplaces.tasks.coalesced_flush_task.apply_async',
            side_effect=RuntimeError('accepted-but-client-error'),
        ),
    ):
        with pytest.raises(RuntimeError, match='accepted-but-client-error'):
            request_feed_flush(account)

    account.refresh_from_db()
    assert account.feed_intent_revision == 1
    assert account.feed_intent_dispatched_revision == 0
    assert account.feed_intent_due_at is not None


def test_dual_write_request_captures_without_double_bump_and_sets_repair_lease(
    settings,
):
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'dual_write'
    due_at = timezone.now() - timedelta(seconds=1)
    account = _account(
        'dual-capture',
        revision=4,
        dispatched_revision=3,
        due_at=due_at,
    )

    with (
        patch(
            'apps.marketplaces.tasks._cache_add_feed_flush_owner',
            return_value=True,
        ),
        patch('apps.marketplaces.tasks._cache_refresh_feed_flush_owner'),
        patch(
            'apps.marketplaces.tasks.coalesced_flush_task.apply_async',
        ) as publish,
    ):
        request_feed_flush(account)

    account.refresh_from_db()
    args = publish.call_args.kwargs['args']
    assert args[:2] == [account.pk, 4]
    assert isinstance(args[2], str) and args[2]
    assert publish.call_args.kwargs['countdown'] == 0
    assert publish.call_args.kwargs['expires'] < 300
    assert account.feed_intent_revision == 4
    assert account.feed_intent_dispatched_revision == 3
    assert account.feed_intent_due_at > due_at


def test_followup_feed_waits_for_hourly_window_but_keeps_exact_revision(settings):
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'dual_write'
    flushed_at = timezone.now()
    account = _account(
        'hourly-followup',
        revision=5,
        dispatched_revision=4,
        due_at=flushed_at,
    )
    MarketplaceAccount.objects.filter(pk=account.pk).update(
        last_feed_flush_at=flushed_at,
    )
    account.refresh_from_db()

    with (
        patch(
            'apps.marketplaces.tasks._cache_add_feed_flush_owner',
            return_value=True,
        ),
        patch('apps.marketplaces.tasks._cache_refresh_feed_flush_owner'),
        patch(
            'apps.marketplaces.tasks.coalesced_flush_task.apply_async',
        ) as publish,
    ):
        request_feed_flush(account)

    account.refresh_from_db()
    args = publish.call_args.kwargs['args']
    countdown = publish.call_args.kwargs['countdown']
    assert args[:2] == [account.pk, 5]
    assert 3590 <= countdown <= 3600
    assert account.feed_intent_revision == 5
    assert account.feed_intent_dispatched_revision == 4
    assert account.feed_intent_due_at >= flushed_at + timedelta(hours=1)


def test_legacy_scanner_schedules_exact_coordinator_not_private_worker():
    due_at = timezone.now() - timedelta(seconds=1)
    account = _account(
        'scanner-exact',
        revision=2,
        dispatched_revision=1,
        due_at=due_at,
    )

    with (
        patch(
            'apps.marketplaces.tasks._cache_add_feed_flush_owner',
            return_value=True,
        ),
        patch('apps.marketplaces.tasks._cache_refresh_feed_flush_owner'),
        patch(
            'apps.marketplaces.tasks.coalesced_flush_task.apply_async',
        ) as publish,
        patch('apps.marketplaces.tasks.enqueue_durable_task') as private_worker,
    ):
        result = dispatch_due_marketplace_feed_intents()

    account.refresh_from_db()
    assert result['status'] == 'legacy_repair'
    assert result['enqueued'] == 1
    assert result['revisions'] == [[account.pk, 2]]
    assert publish.call_args.kwargs['args'][:2] == [account.pk, 2]
    assert account.feed_intent_dispatched_revision == 1
    assert account.feed_intent_due_at > due_at
    assert BackgroundJobDispatch.objects.count() == 0
    private_worker.assert_not_called()


def test_scanner_publish_exception_keeps_exact_due_cursor_unchanged():
    due_at = timezone.now() - timedelta(seconds=1)
    account = _account(
        'scanner-publish-failure',
        revision=3,
        dispatched_revision=2,
        due_at=due_at,
    )

    with (
        patch(
            'apps.marketplaces.tasks._cache_add_feed_flush_owner',
            return_value=True,
        ),
        patch('apps.marketplaces.tasks._cache_clear_feed_flush_owner'),
        patch(
            'apps.marketplaces.tasks.coalesced_flush_task.apply_async',
            side_effect=RuntimeError('broker unavailable'),
        ),
    ):
        result = dispatch_due_marketplace_feed_intents()

    account.refresh_from_db()
    assert result['failed'] == 1
    assert result['enqueued'] == 0
    assert account.feed_intent_revision == 3
    assert account.feed_intent_dispatched_revision == 2
    assert account.feed_intent_due_at == due_at


def test_owned_marker_rotates_overdue_row_behind_bounded_scanner_batch():
    oldest_due = timezone.now() - timedelta(minutes=2)
    next_due = timezone.now() - timedelta(minutes=1)
    oldest = _account(
        'hol-oldest',
        revision=1,
        due_at=oldest_due,
    )
    following = _account(
        'hol-following',
        revision=1,
        due_at=next_due,
    )

    with (
        patch(
            'apps.marketplaces.tasks._cache_add_feed_flush_owner',
            side_effect=[False, True],
        ),
        patch('apps.marketplaces.tasks._cache_refresh_feed_flush_owner'),
        patch(
            'apps.marketplaces.tasks.coalesced_flush_task.apply_async',
        ) as publish,
    ):
        first = dispatch_due_marketplace_feed_intents(limit=1)
        second = dispatch_due_marketplace_feed_intents(limit=1)

    oldest.refresh_from_db()
    following.refresh_from_db()
    assert first['owned'] == 1
    assert second['enqueued'] == 1
    assert publish.call_args.kwargs['args'][:2] == [following.pk, 1]
    assert oldest.feed_intent_due_at > timezone.now()
    assert following.feed_intent_due_at > timezone.now()


def test_cache_get_set_delete_outages_are_best_effort():
    broken = MagicMock()
    broken.get.side_effect = ConnectionError('cache get down')
    with patch('apps.marketplaces.tasks.cache', broken):
        assert _cache_get_feed_flush_owner(123) is _CACHE_UNAVAILABLE

    cache_backend = MagicMock()
    cache_backend.get.return_value = 'owner-token'
    cache_backend.set.side_effect = ConnectionError('cache set down')
    cache_backend.delete.side_effect = ConnectionError('cache delete down')
    with (
        patch('apps.marketplaces.tasks.cache', cache_backend),
        patch(
            'apps.marketplaces.tasks._feed_flush_marker_lock',
            return_value=nullcontext(),
        ),
    ):
        _cache_refresh_feed_flush_owner(
            123,
            'owner-token',
            timeout=60,
        )
        _cache_clear_feed_flush_owner(123, 'owner-token')


def test_old_one_argument_message_materializes_revision_and_flushes_once():
    account = _account('rolling-one-arg')
    fake_listing = object()

    with (
        patch(
            'apps.marketplaces.tasks.try_session_advisory_lock',
            side_effect=_advisory_lock,
        ),
        patch(
            'apps.marketplaces.tasks._account_feed_listings',
            return_value=[fake_listing],
        ),
        patch('apps.marketplaces.tasks.AvitoAdapter') as adapter,
        patch('apps.marketplaces.tasks._write_log'),
        patch('apps.marketplaces.tasks.poll_feed_results_task'),
    ):
        first = coalesced_flush_task(account.pk)
        account.refresh_from_db()
        second = coalesced_flush_task(account.pk, account.feed_intent_revision)

    account.refresh_from_db()
    assert first == {'status': 'completed'}
    assert second == {'status': 'already_completed'}
    assert account.feed_intent_revision == 1
    assert account.feed_intent_dispatched_revision == 1
    assert account.feed_intent_due_at is None
    adapter.return_value.flush_feed.assert_called_once_with([fake_listing])


def test_concurrent_newer_revision_is_not_acknowledged_by_old_provider_snapshot():
    due_at = timezone.now() - timedelta(seconds=1)
    account = _account(
        'concurrent-newer',
        revision=1,
        due_at=due_at,
    )
    fake_listing = object()

    def provider_then_newer_intent(_account):
        MarketplaceAccount.objects.filter(pk=account.pk).update(
            feed_intent_revision=2,
            feed_intent_due_at=timezone.now(),
        )

    with (
        patch(
            'apps.marketplaces.tasks.try_session_advisory_lock',
            side_effect=_advisory_lock,
        ),
        patch(
            'apps.marketplaces.tasks._account_feed_listings',
            return_value=[fake_listing],
        ),
        patch(
            'apps.marketplaces.tasks._flush_account_or_stop',
            side_effect=provider_then_newer_intent,
        ),
        patch('apps.marketplaces.tasks._write_log'),
        patch('apps.marketplaces.tasks.poll_feed_results_task'),
    ):
        result = coalesced_flush_task(account.pk, 1)

    account.refresh_from_db()
    assert result == {'status': 'superseded'}
    assert account.feed_intent_revision == 2
    assert account.feed_intent_dispatched_revision == 1
    assert account.feed_intent_due_at is not None


def test_ambiguous_submission_holds_cursor_and_duplicate_never_reposts():
    account = _account(
        'ambiguous-hold',
        revision=1,
        due_at=timezone.now() - timedelta(seconds=1),
    )
    fake_listing = object()

    with (
        patch(
            'apps.marketplaces.tasks.try_session_advisory_lock',
            side_effect=_advisory_lock,
        ),
        patch(
            'apps.marketplaces.tasks._account_feed_listings',
            return_value=[fake_listing],
        ),
        patch('apps.marketplaces.tasks.AvitoAdapter') as adapter,
        patch('apps.marketplaces.tasks._write_log'),
    ):
        adapter.return_value.flush_feed.side_effect = (
            AmbiguousFeedSubmissionError('timeout after POST')
        )
        first = coalesced_flush_task(account.pk, 1)
        second = coalesced_flush_task(account.pk, 1)

    account.refresh_from_db()
    assert first == {'status': 'outcome_uncertain'}
    assert second == {'status': 'outcome_uncertain'}
    assert account.feed_intent_dispatched_revision == 0
    assert account.feed_intent_due_at is None
    adapter.return_value.flush_feed.assert_called_once()


def test_safe_rate_retry_releases_hold_for_concurrent_newer_revision():
    account = _account(
        'safe-retry-newer',
        revision=1,
        due_at=timezone.now() - timedelta(seconds=1),
    )
    fake_listing = object()

    def provider_rejects_after_newer_intent(_account):
        MarketplaceAccount.objects.filter(pk=account.pk).update(
            feed_intent_revision=2,
            feed_intent_due_at=None,
        )
        raise RateLimitError(retry_after=60)

    with (
        patch(
            'apps.marketplaces.tasks.try_session_advisory_lock',
            side_effect=_advisory_lock,
        ),
        patch(
            'apps.marketplaces.tasks._account_feed_listings',
            return_value=[fake_listing],
        ),
        patch(
            'apps.marketplaces.tasks._flush_account_or_stop',
            side_effect=provider_rejects_after_newer_intent,
        ),
        patch.object(
            coalesced_flush_task,
            'retry',
            side_effect=Retry('replacement accepted'),
        ) as retry,
    ):
        with pytest.raises(Retry):
            coalesced_flush_task(account.pk, 1)

    account.refresh_from_db()
    assert account.feed_intent_revision == 2
    assert account.feed_intent_dispatched_revision == 0
    assert account.feed_intent_due_at > timezone.now()
    assert retry.call_args.kwargs['expires'] < (
        retry.call_args.kwargs['countdown'] + 300
    )


def test_safe_retry_broker_crash_leaves_db_cursor_scanner_repairable():
    account = _account(
        'safe-retry-broker-crash',
        revision=1,
        due_at=timezone.now() - timedelta(seconds=1),
    )
    fake_listing = object()

    with (
        patch(
            'apps.marketplaces.tasks.try_session_advisory_lock',
            side_effect=_advisory_lock,
        ),
        patch(
            'apps.marketplaces.tasks._account_feed_listings',
            return_value=[fake_listing],
        ),
        patch(
            'apps.marketplaces.tasks._flush_account_or_stop',
            side_effect=RateLimitError(retry_after=60),
        ),
        patch.object(
            coalesced_flush_task,
            'retry',
            side_effect=RuntimeError('worker killed after broker acceptance'),
        ),
    ):
        with pytest.raises(RuntimeError, match='worker killed'):
            coalesced_flush_task(account.pk, 1)

    account.refresh_from_db()
    assert account.feed_intent_dispatched_revision == 0
    assert account.feed_intent_due_at > timezone.now()

    MarketplaceAccount.objects.filter(pk=account.pk).update(
        feed_intent_due_at=timezone.now() - timedelta(seconds=1),
    )
    with (
        patch(
            'apps.marketplaces.tasks._cache_add_feed_flush_owner',
            return_value=True,
        ),
        patch('apps.marketplaces.tasks._cache_refresh_feed_flush_owner'),
        patch(
            'apps.marketplaces.tasks.coalesced_flush_task.apply_async',
        ) as publish,
    ):
        repaired = dispatch_due_marketplace_feed_intents()

    assert repaired['enqueued'] == 1
    assert publish.call_args.kwargs['args'][:2] == [account.pk, 1]


def test_success_then_completion_crash_leaves_hold_and_scanner_does_not_repost():
    account = _account(
        'completion-crash',
        revision=1,
        due_at=timezone.now() - timedelta(seconds=1),
    )
    fake_listing = object()

    with (
        patch(
            'apps.marketplaces.tasks.try_session_advisory_lock',
            side_effect=_advisory_lock,
        ),
        patch(
            'apps.marketplaces.tasks._account_feed_listings',
            return_value=[fake_listing],
        ),
        patch('apps.marketplaces.tasks.AvitoAdapter') as adapter,
        patch(
            'apps.marketplaces.tasks._finish_owned_feed_flush',
            side_effect=RuntimeError('database unavailable after POST'),
        ),
    ):
        with pytest.raises(RuntimeError, match='database unavailable after POST'):
            coalesced_flush_task(account.pk, 1)

    account.refresh_from_db()
    assert account.feed_intent_dispatched_revision == 0
    assert account.feed_intent_due_at is None
    assert dispatch_due_marketplace_feed_intents()['selected'] == 0
    adapter.return_value.flush_feed.assert_called_once()


def test_new_request_cannot_release_existing_provider_outcome_hold():
    account = _account(
        'held-request',
        revision=1,
        due_at=None,
    )

    with patch(
        'apps.marketplaces.tasks.coalesced_flush_task.apply_async',
    ) as publish:
        request_feed_flush(account)

    account.refresh_from_db()
    assert account.feed_intent_revision == 2
    assert account.feed_intent_dispatched_revision == 0
    assert account.feed_intent_due_at is None
    publish.assert_not_called()


def test_exhausted_safe_upload_failure_keeps_cursor_without_status_fanout():
    account = _account(
        'safe-upload-failure',
        revision=1,
        due_at=timezone.now() - timedelta(seconds=1),
    )
    pending = _pending_listings(account, count=2)

    with (
        patch(
            'apps.marketplaces.tasks.try_session_advisory_lock',
            side_effect=_advisory_lock,
        ),
        patch('apps.marketplaces.tasks.AvitoAdapter') as adapter,
        patch(
            'apps.marketplaces.tasks._flush_account_or_stop',
            side_effect=FeedUploadError('S3 rejected before provider POST'),
        ),
        patch('apps.marketplaces.tasks._reject_listing') as reject_listing,
        patch('apps.marketplaces.tasks._write_log') as write_log,
        patch.object(coalesced_flush_task, 'max_retries', 0),
    ):
        adapter.return_value.is_autoload_active.return_value = True
        result = coalesced_flush_task(account.pk, 1)

    account.refresh_from_db()
    for listing in pending:
        listing.refresh_from_db()
    assert result == {'status': 'retry_wait'}
    assert account.feed_intent_revision == 1
    assert account.feed_intent_dispatched_revision == 0
    assert account.feed_intent_due_at > timezone.now()
    assert {listing.status for listing in pending} == {Listing.STATUS_PENDING}
    reject_listing.assert_not_called()
    write_log.assert_called_once()


def test_inactive_autoload_rejects_batch_with_one_revision_and_one_digest(
    settings,
):
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'dual_write'
    account = _account(
        'inactive-profile-batch',
        revision=1,
        due_at=timezone.now() - timedelta(seconds=1),
    )
    pending = _pending_listings(account, count=2)

    with (
        patch(
            'apps.marketplaces.tasks.try_session_advisory_lock',
            side_effect=_advisory_lock,
        ),
        patch('apps.marketplaces.tasks.AvitoAdapter') as adapter,
        patch('apps.marketplaces.tasks._notify_error') as notify,
        patch('apps.marketplaces.tasks._flush_account_or_stop') as provider,
    ):
        adapter.return_value.is_autoload_active.return_value = False
        result = coalesced_flush_task(account.pk, 1)

    account.refresh_from_db()
    for listing in pending:
        listing.refresh_from_db()
    assert result == {'status': 'superseded', 'rejected': 2}
    assert account.feed_intent_revision == 2
    assert account.feed_intent_dispatched_revision == 1
    assert account.feed_intent_due_at is not None
    assert {listing.status for listing in pending} == {Listing.STATUS_REJECTED}
    notify.assert_called_once()
    provider.assert_not_called()
