import datetime
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.marketplaces.models import Listing, MarketplaceAccount
from apps.products.models import Product
from apps.tenants.models import Tenant


pytestmark = pytest.mark.django_db
_DEFAULT_EXTERNAL_ID = object()


@pytest.fixture(autouse=True)
def _dual_write_mode(settings):
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'dual_write'


def _listing(
    suffix: str,
    *,
    status: str = Listing.STATUS_ACTIVE,
    external_id: str | None | object = _DEFAULT_EXTERNAL_ID,
) -> Listing:
    tenant = Tenant.objects.create(name=f'Fence {suffix}', slug=f'fence-{suffix}')
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        name='Avito',
        external_id=f'account-{suffix}',
        credentials_enc=b'opaque-test-credentials',
    )
    product = Product.objects.create(
        tenant=tenant,
        article=f'FENCE-{suffix}',
        name=f'Fence product {suffix}',
        price=Decimal('1000.00'),
    )
    resolved_external_id = (
        f'listing-{suffix}'
        if external_id is _DEFAULT_EXTERNAL_ID
        else external_id
    )
    assert isinstance(resolved_external_id, str) or resolved_external_id is None
    return Listing.objects.create(
        tenant=tenant,
        product=product,
        account=account,
        status=status,
        external_id=resolved_external_id,
        price_on_listing=Decimal('1100.00'),
    )


def test_moderation_success_dual_writes_observation_and_active_due():
    from apps.marketplaces.tasks import check_moderation_task

    listing = _listing('active-success')

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_status',
            return_value={'status': 'active'},
        ),
        patch('apps.marketplaces.tasks._write_log') as write_log,
    ):
        result = check_moderation_task(listing.pk)

    listing.refresh_from_db()
    listing.account.refresh_from_db()
    assert result == {'status': 'active', 'changed': False}
    assert listing.remote_status == Listing.REMOTE_STATUS_ACTIVE
    assert listing.remote_status_checked_at is not None
    assert datetime.timedelta(hours=23, minutes=59) <= (
        listing.next_status_check_at - listing.remote_status_checked_at
    ) <= datetime.timedelta(hours=24, seconds=1)
    assert listing.status_check_claim_token is None
    assert listing.status_check_claimed_until is None
    assert listing.account.status_batch_due_at == listing.next_status_check_at
    write_log.assert_not_called()


def test_legacy_active_moderation_queues_bounded_expiry_notice(settings):
    from apps.marketplaces.tasks import check_moderation_task

    listing = _listing('legacy-expiry-notice')
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'legacy'
    checked_at = timezone.now().replace(microsecond=0)
    finish_time = checked_at + datetime.timedelta(days=6)

    with (
        patch('apps.marketplaces.tasks.now', return_value=checked_at),
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_status',
            return_value={
                'status': 'active',
                'finish_time': finish_time.isoformat(),
            },
        ),
        patch('apps.marketplaces.tasks.cache.add', return_value=True) as cache_add,
        patch(
            'apps.notifications.tasks.send_notification_task.delay',
        ) as notify,
    ):
        result = check_moderation_task(listing.pk)

    listing.refresh_from_db()
    assert result == {'status': 'active', 'changed': False}
    assert listing.last_sync_at == checked_at
    cache_add.assert_called_once()
    notify.assert_called_once()
    assert notify.call_args.args[0] == listing.tenant_id
    assert notify.call_args.args[1] == 'error'
    assert 'осталось 6 дн.' in notify.call_args.args[2]
    assert notify.call_args.args[3] == {
        'account_id': listing.account_id,
        'listing_id': listing.pk,
        'finish_time': finish_time.isoformat(),
        'days_left': 6,
    }
    assert notify.call_args.kwargs['event_key'].endswith(':7')


def test_expired_active_listing_notice_is_critical():
    from apps.marketplaces.tasks import _queue_listing_expiry_notification

    listing = _listing('expired-expiry-notice')
    checked_at = timezone.now().replace(microsecond=0)
    finish_time = checked_at - datetime.timedelta(minutes=1)

    with (
        patch('apps.marketplaces.tasks.cache.add', return_value=True),
        patch(
            'apps.notifications.tasks.send_notification_task.delay',
        ) as notify,
    ):
        _queue_listing_expiry_notification(
            listing,
            {'status': 'active', 'finish_time': finish_time.isoformat()},
            checked_at=checked_at,
        )

    notify.assert_called_once()
    assert notify.call_args.args[1] == 'critical'
    assert 'срок размещения объявления' in notify.call_args.args[2]
    assert 'API пока возвращает статус active' in notify.call_args.args[2]
    assert notify.call_args.args[3]['days_left'] == 0
    assert notify.call_args.kwargs['event_key'].endswith(':0')


def test_provider_finish_time_uses_moscow_for_naive_avito_timestamp():
    from apps.marketplaces.tasks import _provider_finish_time

    parsed = _provider_finish_time('2026-09-12T00:52:46')

    assert parsed is not None
    assert timezone.is_aware(parsed)
    assert parsed.utcoffset() == datetime.timedelta(hours=3)


@pytest.mark.parametrize('finish_time', [None, '', 'not-a-date', 17, 'x' * 65])
def test_expiry_notice_ignores_missing_or_malformed_finish_time(finish_time):
    from apps.marketplaces.tasks import _queue_listing_expiry_notification

    listing = _listing(f'bad-expiry-{str(finish_time)[:8]}')
    with (
        patch('apps.marketplaces.tasks.cache.add') as cache_add,
        patch(
            'apps.notifications.tasks.send_notification_task.delay',
        ) as notify,
    ):
        _queue_listing_expiry_notification(
            listing,
            {'status': 'active', 'finish_time': finish_time},
            checked_at=timezone.now(),
        )

    cache_add.assert_not_called()
    notify.assert_not_called()


def test_expiry_notice_is_not_queued_before_fourteen_day_window():
    from apps.marketplaces.tasks import _queue_listing_expiry_notification

    listing = _listing('future-expiry-notice')
    checked_at = timezone.now().replace(microsecond=0)
    finish_time = checked_at + datetime.timedelta(days=15)
    with (
        patch('apps.marketplaces.tasks.cache.add') as cache_add,
        patch(
            'apps.notifications.tasks.send_notification_task.delay',
        ) as notify,
    ):
        _queue_listing_expiry_notification(
            listing,
            {'status': 'active', 'finish_time': finish_time.isoformat()},
            checked_at=checked_at,
        )

    cache_add.assert_not_called()
    notify.assert_not_called()


def test_repeated_expiry_check_is_coalesced_before_notification_queue():
    from apps.marketplaces.tasks import _queue_listing_expiry_notification

    listing = _listing('coalesced-expiry-notice')
    checked_at = timezone.now().replace(microsecond=0)
    response = {
        'status': 'active',
        'finish_time': (checked_at + datetime.timedelta(days=3)).isoformat(),
    }
    with (
        patch(
            'apps.marketplaces.tasks.cache.add',
            side_effect=[True, False],
        ),
        patch(
            'apps.notifications.tasks.send_notification_task.delay',
        ) as notify,
    ):
        _queue_listing_expiry_notification(
            listing,
            response,
            checked_at=checked_at,
        )
        _queue_listing_expiry_notification(
            listing,
            response,
            checked_at=checked_at,
        )

    notify.assert_called_once()
    assert notify.call_args.kwargs['event_key'].endswith(':3')


def test_moderation_stale_response_cannot_resurrect_archiving_intent():
    from apps.marketplaces.tasks import check_moderation_task

    listing = _listing('stale-archive')

    def archive_while_http_is_in_flight(_adapter, _listing):
        Listing.objects.filter(pk=listing.pk).update(
            status=Listing.STATUS_ARCHIVING,
            status_check_claim_token=None,
            status_check_claimed_until=None,
        )
        return {
            'status': 'active',
            'finish_time': (timezone.now() + datetime.timedelta(days=1)).isoformat(),
        }

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_status',
            autospec=True,
            side_effect=archive_while_http_is_in_flight,
        ),
        patch('apps.marketplaces.tasks._write_log') as write_log,
        patch('apps.marketplaces.tasks._notify_error') as notify_error,
        patch(
            'apps.notifications.tasks.send_notification_task.delay',
        ) as expiry_notify,
    ):
        result = check_moderation_task(listing.pk)

    listing.refresh_from_db()
    assert result == {'status': 'stale', 'changed': False}
    assert listing.status == Listing.STATUS_ARCHIVING
    assert listing.remote_status is None
    assert listing.remote_status_checked_at is None
    write_log.assert_not_called()
    notify_error.assert_not_called()
    expiry_notify.assert_not_called()


def test_live_claim_suppresses_duplicate_provider_call():
    from apps.marketplaces.tasks import check_moderation_task

    listing = _listing('duplicate-claim')
    listing.status_check_claim_token = uuid4()
    listing.status_check_claimed_until = timezone.now() + datetime.timedelta(minutes=2)
    listing.save(update_fields=['status_check_claim_token', 'status_check_claimed_until'])

    with patch('apps.marketplaces.tasks.AvitoAdapter.get_status') as get_status:
        result = check_moderation_task(listing.pk)

    assert result == {'status': 'skipped', 'reason': 'already_claimed'}
    get_status.assert_not_called()


def test_unknown_provider_status_records_other_without_canonical_transition():
    from apps.marketplaces.tasks import check_moderation_task

    listing = _listing('unknown', status=Listing.STATUS_REJECTED)

    with patch(
        'apps.marketplaces.tasks.AvitoAdapter.get_status',
        return_value={'status': 'future-provider-state'},
    ):
        result = check_moderation_task(listing.pk)

    listing.refresh_from_db()
    assert result == {
        'status': 'ignored',
        'provider_status': Listing.REMOTE_STATUS_OTHER,
    }
    assert listing.status == Listing.STATUS_REJECTED
    assert listing.remote_status == Listing.REMOTE_STATUS_OTHER
    assert listing.next_status_check_at is not None


def test_confirm_removal_stale_response_has_no_archive_log():
    from apps.marketplaces.tasks import confirm_removal_task

    listing = _listing('removal-stale', status=Listing.STATUS_ARCHIVING)

    def republish_while_http_is_in_flight(_adapter, _listing):
        Listing.objects.filter(pk=listing.pk).update(
            status=Listing.STATUS_ACTIVE,
            external_id='replacement-id',
            status_check_claim_token=None,
            status_check_claimed_until=None,
        )
        return {'status': 'old'}

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_status',
            autospec=True,
            side_effect=republish_while_http_is_in_flight,
        ),
        patch('apps.marketplaces.tasks._write_log') as write_log,
    ):
        result = confirm_removal_task(listing.pk)

    listing.refresh_from_db()
    assert result == {'status': 'stale', 'changed': False}
    assert listing.status == Listing.STATUS_ACTIVE
    assert listing.external_id == 'replacement-id'
    assert listing.remote_status is None
    write_log.assert_not_called()


def test_feed_result_stale_response_cannot_activate_rejected_listing():
    from apps.marketplaces.adapters.avito.feed_builder import get_ad_id
    from apps.marketplaces.tasks import poll_feed_results_task

    listing = _listing('feed-stale', status=Listing.STATUS_PENDING, external_id=None)

    def reject_while_http_is_in_flight(_adapter, _ad_ids):
        Listing.objects.filter(pk=listing.pk).update(
            status=Listing.STATUS_REJECTED,
            rejection_reason='new local rejection',
            status_check_claim_token=None,
            status_check_claimed_until=None,
        )
        return [{'ad_id': get_ad_id(listing), 'avito_id': 123456}]

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_feed_results',
            autospec=True,
            side_effect=reject_while_http_is_in_flight,
        ),
        patch('apps.marketplaces.tasks._notify_success') as notify_success,
        patch('apps.marketplaces.tasks._write_log') as write_log,
    ):
        poll_feed_results_task(listing.account_id)

    listing.refresh_from_db()
    assert listing.status == Listing.STATUS_REJECTED
    assert listing.external_id is None
    assert listing.remote_status is None
    notify_success.assert_not_called()
    write_log.assert_not_called()


def test_feed_error_reason_is_bounded_and_notification_waits_for_cas():
    from apps.marketplaces.adapters.avito.adapter import FeedUploadError
    from apps.marketplaces.tasks import poll_feed_results_task

    listing = _listing('feed-error', status=Listing.STATUS_PENDING, external_id=None)
    unsafe_reason = 'provider\n\x00error ' * 500

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_feed_results',
            side_effect=FeedUploadError(unsafe_reason),
        ),
        patch('apps.marketplaces.tasks._notify_error') as notify_error,
    ):
        poll_feed_results_task(listing.account_id)

    listing.refresh_from_db()
    assert listing.status == Listing.STATUS_REJECTED
    assert len(listing.rejection_reason) <= 2000
    assert '\n' not in listing.rejection_reason
    assert '\x00' not in listing.rejection_reason
    notify_error.assert_called_once()


def test_feed_poll_uses_durable_row_due_instead_of_task_retry_budget():
    from apps.marketplaces.tasks import poll_feed_results_task

    listing = _listing('feed-pacing', status=Listing.STATUS_PENDING, external_id=None)

    with (
        patch('apps.marketplaces.tasks.AvitoAdapter.get_feed_results', return_value=[]),
        patch(
            'apps.marketplaces.tasks._feed_errors_are_current',
            return_value=True,
        ),
        patch(
            'apps.marketplaces.tasks.schedule_avito_feed_item_error_reconciliation',
        ) as schedule_report,
        patch('apps.marketplaces.tasks.poll_feed_results_task.apply_async') as schedule_poll,
    ):
        result = poll_feed_results_task(listing.account_id)

    listing.refresh_from_db()
    listing.account.refresh_from_db()
    assert result == {
        'status': 'processed',
        'total': 1,
        'published': 0,
        'rejected': 0,
        'pending': 1,
    }
    assert datetime.timedelta(minutes=29, seconds=50) <= (
        listing.next_status_check_at - timezone.now()
    ) <= datetime.timedelta(minutes=30, seconds=1)
    assert listing.account.status_batch_due_at is None
    schedule_report.assert_called_once_with(listing.account)
    schedule_poll.assert_called_once_with(
        args=[listing.account_id],
        countdown=30 * 60,
    )


def test_feed_poll_claims_at_most_one_hundred_rows_per_provider_request():
    from apps.marketplaces.tasks import poll_feed_results_task

    first = _listing('feed-batch-0', status=Listing.STATUS_PENDING, external_id=None)
    for index in range(1, 101):
        product = Product.objects.create(
            tenant=first.tenant,
            article=f'FENCE-BATCH-{index}',
            name=f'Fence batch product {index}',
            price=Decimal('1000.00'),
        )
        Listing.objects.create(
            tenant=first.tenant,
            product=product,
            account=first.account,
            status=Listing.STATUS_PENDING,
            price_on_listing=Decimal('1100.00'),
        )

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_feed_results',
            return_value=[],
        ) as get_results,
        patch('apps.marketplaces.tasks.poll_feed_results_task.apply_async') as schedule_poll,
    ):
        result = poll_feed_results_task(first.account_id)

    assert result['total'] == 100
    assert len(get_results.call_args.args[0]) == 100
    assert Listing.objects.filter(
        account=first.account,
        status=Listing.STATUS_PENDING,
        next_status_check_at__isnull=True,
    ).count() == 1
    schedule_poll.assert_called_once_with(
        args=[first.account_id],
        countdown=30,
    )


def test_local_task_intent_revokes_claim_and_nudges_account_due():
    from apps.marketplaces.tasks import unpublish_listing_task

    listing = _listing('local-intent')
    listing.status_check_claim_token = uuid4()
    listing.status_check_claimed_until = timezone.now() + datetime.timedelta(minutes=4)
    listing.save(update_fields=['status_check_claim_token', 'status_check_claimed_until'])

    with (
        patch('apps.marketplaces.tasks._write_log'),
        patch('apps.marketplaces.tasks.request_feed_flush'),
    ):
        unpublish_listing_task(listing.pk)

    listing.refresh_from_db()
    listing.account.refresh_from_db()
    assert listing.status == Listing.STATUS_ARCHIVING
    assert listing.status_check_claim_token is None
    assert listing.status_check_claimed_until is None
    assert listing.next_status_check_at is not None
    assert listing.account.status_batch_due_at == listing.next_status_check_at


def test_service_intent_revokes_claim_before_scheduling_provider_work():
    from apps.marketplaces.services import ListingService

    listing = _listing('service-intent')
    listing.status_check_claim_token = uuid4()
    listing.status_check_claimed_until = timezone.now() + datetime.timedelta(minutes=4)
    listing.save(update_fields=['status_check_claim_token', 'status_check_claimed_until'])

    ListingService.archive(listing.pk, listing.tenant)

    listing.refresh_from_db()
    listing.account.refresh_from_db()
    assert listing.status == Listing.STATUS_ARCHIVING
    assert listing.status_check_claim_token is None
    assert listing.status_check_claimed_until is None
    assert listing.next_status_check_at is not None
    assert listing.account.status_batch_due_at == listing.next_status_check_at


def test_service_account_change_clears_old_provider_generation():
    from apps.marketplaces.services import ListingService

    listing = _listing('service-account', status=Listing.STATUS_REJECTED)
    replacement = MarketplaceAccount.objects.create(
        tenant=listing.tenant,
        name='Avito replacement',
        external_id='account-service-account-replacement',
        credentials_enc=b'opaque-test-credentials',
    )
    listing.external_url = 'https://www.avito.ru/old-listing'
    listing.remote_status = Listing.REMOTE_STATUS_REJECTED
    listing.remote_status_checked_at = timezone.now()
    listing.status_check_claim_token = uuid4()
    listing.status_check_claimed_until = timezone.now() + datetime.timedelta(minutes=4)
    listing.save(update_fields=[
        'external_url',
        'remote_status',
        'remote_status_checked_at',
        'status_check_claim_token',
        'status_check_claimed_until',
    ])

    ListingService.update_listing_fields(
        listing.pk,
        listing.tenant,
        {'account_id': replacement.pk},
    )

    listing.refresh_from_db()
    assert listing.account_id == replacement.pk
    assert listing.external_id is None
    assert listing.external_url == ''
    assert listing.remote_status is None
    assert listing.remote_status_checked_at is None
    assert listing.next_status_check_at is None
    assert listing.status_check_claim_token is None
    assert listing.status_check_claimed_until is None


def test_bulk_placement_update_revokes_claims_and_nudges_due():
    from apps.marketplaces.services import ListingService

    listing = _listing('bulk-placement')
    listing.status_check_claim_token = uuid4()
    listing.status_check_claimed_until = timezone.now() + datetime.timedelta(minutes=4)
    listing.save(update_fields=['status_check_claim_token', 'status_check_claimed_until'])

    updated = ListingService.bulk_update_placement(
        listing.tenant,
        {'listing_ids': [listing.pk]},
        {'address_override': 'Москва'},
    )

    listing.refresh_from_db()
    listing.account.refresh_from_db()
    assert updated == 1
    assert listing.bulk_address == 'Москва'
    assert listing.status_check_claim_token is None
    assert listing.status_check_claimed_until is None
    assert listing.next_status_check_at is not None
    assert listing.account.status_batch_due_at == listing.next_status_check_at


def test_product_archive_persists_local_intent_before_dispatch():
    from apps.marketplaces.services import ListingService

    listing = _listing('product-archive')
    listing.status_check_claim_token = uuid4()
    listing.status_check_claimed_until = timezone.now() + datetime.timedelta(minutes=4)
    listing.save(update_fields=['status_check_claim_token', 'status_check_claimed_until'])

    count = ListingService.archive_product(listing.product, listing.tenant)

    listing.refresh_from_db()
    assert count == 1
    assert listing.status == Listing.STATUS_ARCHIVING
    assert listing.status_check_claim_token is None
    assert listing.status_check_claimed_until is None


def test_legacy_moderation_does_not_touch_additive_lifecycle_fields(settings):
    from apps.marketplaces.tasks import check_moderation_task

    listing = _listing('legacy')
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'legacy'

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_status',
            return_value={'status': 'active'},
        ),
        patch('apps.marketplaces.tasks._write_log') as write_log,
    ):
        result = check_moderation_task(listing.pk)

    listing.refresh_from_db()
    assert result == {'status': 'active', 'changed': False}
    assert listing.remote_status is None
    assert listing.remote_status_checked_at is None
    assert listing.next_status_check_at is None
    assert listing.status_check_claim_token is None
    write_log.assert_not_called()


def test_legacy_moderation_logs_only_a_real_status_transition(settings):
    from apps.marketplaces.tasks import check_moderation_task

    listing = _listing('legacy-transition', status=Listing.STATUS_PENDING)
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'legacy'

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_status',
            return_value={'status': 'active'},
        ),
        patch('apps.marketplaces.tasks._write_log') as write_log,
    ):
        result = check_moderation_task(listing.pk)

    listing.refresh_from_db()
    assert result == {'status': 'active', 'changed': True}
    assert listing.status == Listing.STATUS_ACTIVE
    write_log.assert_called_once()


def test_legacy_rejection_does_not_repeat_log_or_notification(settings):
    from apps.marketplaces.tasks import check_moderation_task

    listing = _listing('legacy-rejected', status=Listing.STATUS_REJECTED)
    listing.rejection_reason = 'Неверная категория'
    listing.save(update_fields=['rejection_reason'])
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'legacy'

    with (
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_status',
            return_value={
                'status': 'rejected',
                'rejection_reason': 'Неверная категория',
            },
        ),
        patch('apps.marketplaces.tasks._write_log') as write_log,
        patch('apps.marketplaces.tasks._notify_error') as notify_error,
    ):
        result = check_moderation_task(listing.pk)

    assert result == {'status': 'rejected', 'changed': False}
    write_log.assert_not_called()
    notify_error.assert_not_called()
