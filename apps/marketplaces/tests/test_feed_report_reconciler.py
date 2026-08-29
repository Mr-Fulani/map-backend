import contextlib
import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.marketplaces.adapters.avito.adapter import (
    AvitoAdapter,
    FeedItemErrorPage,
    FeedItemOutcomePage,
)
from apps.marketplaces.feed_report_reconciler import (
    FEED_REPORT_MAX_RETRY_DELAY_SECONDS,
    _account_identity_marker,
    _flush_marker,
    reconcile_avito_feed_item_errors_page_task,
    schedule_avito_feed_item_error_reconciliation,
)
from apps.marketplaces.models import Listing, MarketplaceAccount
from apps.products.models import Product
from apps.sync.models import SyncLog
from apps.tenants.models import Tenant


class TestAvitoFeedItemErrorPage:
    @staticmethod
    def _adapter():
        adapter = object.__new__(AvitoAdapter)
        adapter.account = MagicMock()
        adapter._auth = MagicMock()
        adapter._rl = MagicMock()
        adapter._auth.get_token.return_value = 'token'
        return adapter

    def test_fetches_exactly_one_bounded_sanitized_page(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            'items': [
                {
                    'ad_id': 'wanted-id',
                    'messages': [
                        {
                            'type': 'error',
                            'title': '<b>Bad\x00 field</b>',
                            'description': '<i>Wrong</i> &amp; unsafe' + (' x' * 2000),
                        },
                        {
                            'type': 'alarm',
                            'title': 'Context',
                            'description': 'Details',
                        },
                    ],
                },
                {
                    'ad_id': 'warning-only',
                    'messages': [{'type': 'warning', 'title': 'Not blocking'}],
                },
            ],
            'meta': {'page': 3, 'per_page': 100, 'pages': 5},
        }

        with patch(
            'apps.marketplaces.adapters.avito.adapter._avito_request',
            return_value=response,
        ) as request:
            result = self._adapter().get_feed_item_error_page(3)

        assert request.call_count == 1
        assert request.call_args.kwargs['params'] == {'per_page': 100, 'page': 3}
        assert result.next_page == 4
        assert result.terminal is False
        assert set(result.errors) == {'wanted-id'}
        assert len(result.errors['wanted-id']) <= 2000
        assert '<' not in result.errors['wanted-id']
        assert '\x00' not in result.errors['wanted-id']
        assert '&amp;' not in result.errors['wanted-id']

    def test_rejects_oversized_page_after_one_request(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            'items': [{'ad_id': str(index), 'messages': []} for index in range(101)],
            'meta': {'pages': 1},
        }

        with patch(
            'apps.marketplaces.adapters.avito.adapter._avito_request',
            return_value=response,
        ) as request:
            with pytest.raises(ValueError, match='exceeds 100 items'):
                self._adapter().get_feed_item_error_page(1)

        request.assert_called_once()

    @pytest.mark.parametrize('meta', [
        [],
        {'pages': True},
        {'pages': 0},
        {'pages': 2, 'page': 2},
        {'pages': 2, 'per_page': 101},
    ])
    def test_rejects_malformed_pagination_metadata(self, meta):
        response = MagicMock(status_code=200)
        response.json.return_value = {'items': [], 'meta': meta}

        with patch(
            'apps.marketplaces.adapters.avito.adapter._avito_request',
            return_value=response,
        ):
            with pytest.raises(ValueError):
                self._adapter().get_feed_item_error_page(1)

    def test_401_invalidates_token_without_hidden_second_request(self):
        response = MagicMock(status_code=401, ok=False, text='expired', headers={})
        adapter = self._adapter()

        with patch(
            'apps.marketplaces.adapters.avito.adapter._avito_request',
            return_value=response,
        ) as request:
            with pytest.raises(Exception, match='Токен истёк'):
                adapter.get_feed_item_error_page(1)

        request.assert_called_once()
        adapter._auth.invalidate.assert_called_once_with(adapter.account)

    def test_current_page_returns_only_proven_active_ids_and_blocking_errors(self):
        response = MagicMock(status_code=200, ok=True)
        response.json.return_value = {
            'items': [
                {
                    'ad_id': 'active-id',
                    'avito_status': 'active',
                    'avito_id': 8273167174,
                    'messages': [{'type': 'warning', 'title': 'Adjusted'}],
                },
                {
                    'ad_id': 'error-id',
                    'avito_status': 'active',
                    'avito_id': 8385631878,
                    'messages': [{
                        'type': 'error',
                        'title': 'Неверный OEM',
                        'description': 'Исправьте обязательное поле',
                    }],
                },
                {
                    'ad_id': 'unresolved-id',
                    'avito_status': 'processing',
                    'avito_id': None,
                    'messages': [],
                },
            ],
            'meta': {'page': 1, 'perPage': 100, 'pages': 1},
        }

        with patch(
            'apps.marketplaces.adapters.avito.adapter._avito_request',
            return_value=response,
        ) as request:
            result = self._adapter().get_current_feed_item_outcome_page(1)

        assert isinstance(result, FeedItemOutcomePage)
        assert request.call_args.args[1].endswith('/autoload/v4/uploads/current/items')
        assert request.call_args.kwargs['params'] == {'perPage': 100, 'page': 1}
        assert result.external_ids == {'active-id': '8273167174'}
        assert set(result.errors) == {'error-id'}
        assert 'Неверный OEM' in result.errors['error-id']
        assert result.terminal is True


pytestmark = pytest.mark.django_db


def _account(suffix: str, *, tenant_active: bool = True) -> MarketplaceAccount:
    tenant = Tenant.objects.create(
        name=f'Feed report {suffix}',
        slug=f'feed-report-{suffix}',
        is_active=tenant_active,
    )
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        name='Avito',
        external_id=f'avito-{suffix}',
        credentials_enc=b'opaque-test-credentials',
        last_feed_flush_at=timezone.now(),
    )


def _listing(
    account: MarketplaceAccount,
    suffix: str,
    *,
    status: str = Listing.STATUS_PENDING,
    external_id: str | None = None,
    deleted: bool = False,
) -> Listing:
    product = Product.objects.create(
        tenant=account.tenant,
        article=f'FEED-{suffix}',
        name=f'Feed product {suffix}',
        price=Decimal('1000.00'),
    )
    listing = Listing.objects.create(
        tenant=account.tenant,
        product=product,
        account=account,
        status=status,
        external_id=external_id,
        price_on_listing=Decimal('1100.00'),
    )
    if deleted:
        Listing.all_objects.filter(pk=listing.pk).update(deleted_at=timezone.now())
        listing = Listing.all_objects.get(pk=listing.pk)
    return listing


def _run_page(account: MarketplaceAccount, page_result: FeedItemErrorPage):
    with (
        patch(
            'apps.marketplaces.feed_report_reconciler.try_session_advisory_lock',
            return_value=contextlib.nullcontext(True),
        ),
        patch(
            'apps.marketplaces.feed_report_reconciler.AvitoAdapter.get_feed_item_error_page',
            return_value=page_result,
        ) as provider_page,
        patch(
            'apps.marketplaces.feed_report_reconciler.send_notification_task.delay',
        ) as notify,
        patch(
            'apps.marketplaces.feed_report_reconciler._enqueue_page',
        ) as enqueue,
    ):
        result = reconcile_avito_feed_item_errors_page_task(
            account.pk,
            expected_flush_marker=_flush_marker(account),
        )
    return result, provider_page, notify, enqueue


@pytest.fixture(autouse=True)
def _dual_write(settings):
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'dual_write'


def test_schedule_hook_uses_only_scalar_cursor_payload_and_bounded_delay():
    account = _account('schedule')

    with patch.object(
        reconcile_avito_feed_item_errors_page_task,
        'apply_async',
    ) as apply_async, patch(
        'apps.marketplaces.feed_report_reconciler.coordination_cache.add',
        return_value=True,
    ) as cache_add:
        schedule_avito_feed_item_error_reconciliation(account, countdown=999_999)

    call = apply_async.call_args
    assert call.kwargs['countdown'] == FEED_REPORT_MAX_RETRY_DELAY_SECONDS
    assert call.kwargs['kwargs'] == {
        'account_id': account.pk,
        'page': 1,
        'expected_flush_marker': _flush_marker(account),
        'expected_account_marker': _account_identity_marker(account),
        'attempt': 0,
    }
    assert cache_add.call_args.kwargs['timeout'] == 6 * 60 * 60


def test_schedule_hook_deduplicates_the_same_feed_generation():
    account = _account('schedule-dedup')

    with patch(
        'apps.marketplaces.feed_report_reconciler.coordination_cache.add',
        return_value=False,
    ), patch.object(
        reconcile_avito_feed_item_errors_page_task,
        'apply_async',
    ) as apply_async:
        result = schedule_avito_feed_item_error_reconciliation(account)

    assert result is None
    apply_async.assert_not_called()


def test_page_rejects_only_live_pending_null_external_rows_and_is_idempotent():
    account = _account('apply')
    target = _listing(account, 'target')
    target.next_status_check_at = timezone.now()
    target.status_check_claim_token = uuid4()
    target.status_check_claimed_until = timezone.now()
    target.save(update_fields=[
        'next_status_check_at', 'status_check_claim_token',
        'status_check_claimed_until',
    ])
    active = _listing(account, 'active', status=Listing.STATUS_ACTIVE)
    published = _listing(account, 'published', external_id='already-published')
    deleted = _listing(account, 'deleted', deleted=True)
    other_account = _account('other-account')
    other = _listing(other_account, 'other')
    unsafe_reason = '<b>Invalid</b>\n\x00 field ' + ('x' * 3000)
    errors = {
        str(target.publish_idempotency_key): unsafe_reason,
        str(active.publish_idempotency_key): 'active must stay active',
        str(published.publish_idempotency_key): 'published must stay pending',
        str(deleted.publish_idempotency_key): 'deleted must stay hidden',
        str(other.publish_idempotency_key): 'other account must stay pending',
        'not-a-uuid': 'ignored',
    }

    result, provider_page, notify, enqueue = _run_page(
        account,
        FeedItemErrorPage(errors=errors, next_page=None),
    )

    target.refresh_from_db()
    active.refresh_from_db()
    published.refresh_from_db()
    deleted = Listing.all_objects.get(pk=deleted.pk)
    other.refresh_from_db()
    assert result == {
        'status': 'processed',
        'page': 1,
        'changed': 1,
        'scheduled_next': False,
        'terminal': True,
    }
    provider_page.assert_called_once_with(1)
    enqueue.assert_not_called()
    assert target.status == Listing.STATUS_REJECTED
    assert target.rejection_reason.startswith('Invalid field ')
    assert len(target.rejection_reason) == 2000
    assert target.next_status_check_at is None
    assert target.status_check_claim_token is None
    assert target.status_check_claimed_until is None
    assert active.status == Listing.STATUS_ACTIVE
    assert published.status == Listing.STATUS_PENDING
    assert deleted.status == Listing.STATUS_PENDING
    assert other.status == Listing.STATUS_PENDING
    assert SyncLog.objects.filter(
        listing=target,
        event_type=SyncLog.EVENT_LISTING_ERROR,
        status=SyncLog.STATUS_ERROR,
    ).count() == 1
    notify.assert_called_once()
    assert notify.call_args.args[:2] == (account.tenant_id, 'error')
    assert '1 объявлений' in notify.call_args.args[2]
    assert notify.call_args.kwargs['event_key'].startswith(
        f'avito-feed-report:{account.pk}:'
    )

    result, _provider_page, notify, _enqueue = _run_page(
        account,
        FeedItemErrorPage(errors=errors, next_page=None),
    )
    assert result['changed'] == 0
    assert SyncLog.objects.filter(listing=target).count() == 1
    notify.assert_not_called()


def test_local_intent_change_during_http_fences_report_transition():
    account = _account('intent-race')
    listing = _listing(account, 'intent-race')

    def reject_locally(_adapter, _page):
        Listing.objects.filter(pk=listing.pk).update(
            status=Listing.STATUS_REJECTED,
            rejection_reason='newer local decision',
        )
        return FeedItemErrorPage(
            errors={str(listing.publish_idempotency_key): 'stale report reason'},
            next_page=None,
        )

    with (
        patch(
            'apps.marketplaces.feed_report_reconciler.try_session_advisory_lock',
            return_value=contextlib.nullcontext(True),
        ),
        patch(
            'apps.marketplaces.feed_report_reconciler.AvitoAdapter.get_feed_item_error_page',
            autospec=True,
            side_effect=reject_locally,
        ),
        patch(
            'apps.marketplaces.feed_report_reconciler.send_notification_task.delay',
        ) as notify,
    ):
        result = reconcile_avito_feed_item_errors_page_task(
            account.pk,
            expected_flush_marker=_flush_marker(account),
        )

    listing.refresh_from_db()
    assert result['changed'] == 0
    assert listing.rejection_reason == 'newer local decision'
    assert not SyncLog.objects.filter(listing=listing).exists()
    notify.assert_not_called()


def test_new_feed_marker_during_http_stops_stale_report():
    account = _account('feed-race')
    listing = _listing(account, 'feed-race')
    original_marker = _flush_marker(account)

    def upload_new_feed(_adapter, _page):
        MarketplaceAccount.objects.filter(pk=account.pk).update(
            last_feed_flush_at=timezone.now() + datetime.timedelta(minutes=1),
        )
        return FeedItemErrorPage(
            errors={str(listing.publish_idempotency_key): 'old feed error'},
            next_page=None,
        )

    with (
        patch(
            'apps.marketplaces.feed_report_reconciler.try_session_advisory_lock',
            return_value=contextlib.nullcontext(True),
        ),
        patch(
            'apps.marketplaces.feed_report_reconciler.AvitoAdapter.get_feed_item_error_page',
            autospec=True,
            side_effect=upload_new_feed,
        ),
        patch(
            'apps.marketplaces.feed_report_reconciler._enqueue_page',
        ) as enqueue,
    ):
        result = reconcile_avito_feed_item_errors_page_task(
            account.pk,
            expected_flush_marker=original_marker,
        )

    listing.refresh_from_db()
    assert result == {'status': 'stale_feed'}
    assert listing.status == Listing.STATUS_PENDING
    assert not SyncLog.objects.filter(listing=listing).exists()
    enqueue.assert_not_called()


def test_account_identity_change_during_http_fences_old_provider_response():
    account = _account('identity-race')
    listing = _listing(account, 'identity-race')

    def rotate_credentials(_adapter, _page):
        MarketplaceAccount.objects.filter(pk=account.pk).update(
            credentials_enc=b'new-credential-generation',
        )
        return FeedItemErrorPage(
            errors={str(listing.publish_idempotency_key): 'old identity error'},
            next_page=None,
        )

    with (
        patch(
            'apps.marketplaces.feed_report_reconciler.try_session_advisory_lock',
            return_value=contextlib.nullcontext(True),
        ),
        patch(
            'apps.marketplaces.feed_report_reconciler.AvitoAdapter.get_feed_item_error_page',
            autospec=True,
            side_effect=rotate_credentials,
        ),
        patch(
            'apps.marketplaces.feed_report_reconciler.send_notification_task.delay',
        ) as notify,
    ):
        result = reconcile_avito_feed_item_errors_page_task(
            account.pk,
            expected_flush_marker=_flush_marker(account),
        )

    listing.refresh_from_db()
    assert result == {'status': 'stale_account'}
    assert listing.status == Listing.STATUS_PENDING
    assert not SyncLog.objects.filter(listing=listing).exists()
    notify.assert_not_called()


def test_one_invocation_schedules_only_the_next_page(settings):
    settings.AVITO_API_MAX_PAGES = 2
    account = _account('next-page')

    result, provider_page, notify, enqueue = _run_page(
        account,
        FeedItemErrorPage(errors={}, next_page=2),
    )

    assert result['scheduled_next'] is True
    provider_page.assert_called_once_with(1)
    notify.assert_not_called()
    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs['page'] == 2
    assert enqueue.call_args.kwargs['attempt'] == 0
    assert set(enqueue.call_args.kwargs) == {
        'account_id', 'page', 'expected_flush_marker',
        'expected_account_marker', 'attempt', 'countdown',
    }


def test_hard_page_limit_is_terminal_without_scheduling(settings):
    settings.AVITO_API_MAX_PAGES = 1
    account = _account('page-limit')

    result, _provider_page, _notify, enqueue = _run_page(
        account,
        FeedItemErrorPage(errors={}, next_page=2),
    )

    assert result['terminal'] is True
    assert result['scheduled_next'] is False
    enqueue.assert_not_called()


def test_advisory_contention_is_nonblocking_and_bounded():
    account = _account('advisory-lock')

    with (
        patch(
            'apps.marketplaces.feed_report_reconciler.try_session_advisory_lock',
            return_value=contextlib.nullcontext(False),
        ),
        patch(
            'apps.marketplaces.feed_report_reconciler.AvitoAdapter.get_feed_item_error_page',
        ) as provider_page,
        patch(
            'apps.marketplaces.feed_report_reconciler._enqueue_page',
        ) as enqueue,
    ):
        result = reconcile_avito_feed_item_errors_page_task(
            account.pk,
            expected_flush_marker=_flush_marker(account),
        )

    assert result == {'status': 'locked', 'rescheduled': True}
    provider_page.assert_not_called()
    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs['attempt'] == 1


@pytest.mark.parametrize('account_mutation', ['inactive_account', 'inactive_tenant'])
def test_inactive_account_or_tenant_is_rejected_before_provider_call(account_mutation):
    account = _account(account_mutation)
    if account_mutation == 'inactive_account':
        MarketplaceAccount.objects.filter(pk=account.pk).update(is_active=False)
    else:
        Tenant.objects.filter(pk=account.tenant_id).update(is_active=False)

    with (
        patch(
            'apps.marketplaces.feed_report_reconciler.try_session_advisory_lock',
            return_value=contextlib.nullcontext(True),
        ),
        patch(
            'apps.marketplaces.feed_report_reconciler.AvitoAdapter.get_feed_item_error_page',
        ) as provider_page,
    ):
        result = reconcile_avito_feed_item_errors_page_task(
            account.pk,
            expected_flush_marker=_flush_marker(account),
        )

    assert result == {'status': 'ineligible_account'}
    provider_page.assert_not_called()


def test_legacy_mode_disables_reconciler_before_lock(settings):
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'legacy'

    with patch(
        'apps.marketplaces.feed_report_reconciler.try_session_advisory_lock',
    ) as advisory_lock:
        result = reconcile_avito_feed_item_errors_page_task(1)

    assert result == {'status': 'disabled'}
    advisory_lock.assert_not_called()
