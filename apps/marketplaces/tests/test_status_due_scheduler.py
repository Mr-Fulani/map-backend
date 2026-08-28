import datetime
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.datasources.encryption import encrypt
from apps.marketplaces.models import Listing, MarketplaceAccount
from apps.marketplaces.tasks import (
    _dispatch_due_listing_status_checks,
    check_moderation_status,
)
from apps.products.models import Product
from apps.tenants.models import Tenant


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _dual_write_status_lifecycle(settings):
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'dual_write'


def _account(suffix: str, *, tenant_active: bool = True) -> MarketplaceAccount:
    tenant = Tenant.objects.create(
        name=f'Status due {suffix}',
        slug=f'status-due-{suffix}',
        is_active=tenant_active,
    )
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Avito {suffix}',
        external_id=f'account-{suffix}',
        credentials_enc=encrypt({
            'client_id': f'client-{suffix}',
            'client_secret': f'secret-{suffix}',
        }),
    )


def _listing(
    account: MarketplaceAccount,
    suffix: str,
    *,
    status: str = Listing.STATUS_ACTIVE,
    due_at: datetime.datetime | None,
) -> Listing:
    product = Product.objects.create(
        tenant=account.tenant,
        article=f'STATUS-DUE-{suffix}',
        name=f'Status due product {suffix}',
        price='1000.00',
    )
    return Listing.objects.create(
        tenant=account.tenant,
        product=product,
        account=account,
        status=status,
        external_id=f'listing-{suffix}',
        price_on_listing='1100.00',
        next_status_check_at=due_at,
    )


def _sync_account_due(account: MarketplaceAccount) -> None:
    due = (
        Listing.objects.filter(
            account=account,
            next_status_check_at__isnull=False,
        )
        .order_by('next_status_check_at')
        .values_list('next_status_check_at', flat=True)
        .first()
    )
    MarketplaceAccount.objects.filter(pk=account.pk).update(
        status_batch_due_at=due,
    )


def test_dual_write_scheduler_dispatches_only_due_rows_by_task_type():
    checked_at = timezone.now()
    account = _account('selection')
    due_active = _listing(
        account,
        'due-active',
        due_at=checked_at - datetime.timedelta(minutes=1),
    )
    due_archiving = _listing(
        account,
        'due-archiving',
        status=Listing.STATUS_ARCHIVING,
        due_at=checked_at - datetime.timedelta(seconds=30),
    )
    future = _listing(
        account,
        'future',
        due_at=checked_at + datetime.timedelta(hours=1),
    )
    _listing(account, 'no-cursor', due_at=None)
    _sync_account_due(account)

    with (
        patch('apps.marketplaces.tasks.check_moderation_task.delay') as moderation,
        patch('apps.marketplaces.tasks.confirm_removal_task.delay') as removal,
    ):
        result = _dispatch_due_listing_status_checks()

    assert result == {
        'accounts_claimed': 1,
        'accounts_released': 1,
        'selected': 2,
        'scheduled': 2,
        'moderation_scheduled': 1,
        'removal_scheduled': 1,
        'dispatch_failed': 0,
    }
    moderation.assert_called_once_with(due_active.pk)
    removal.assert_called_once_with(due_archiving.pk)
    assert future.pk not in {
        call.args[0] for call in moderation.call_args_list + removal.call_args_list
    }
    account.refresh_from_db()
    assert account.status_batch_claim_token is None
    assert account.status_batch_claimed_until is None
    assert account.status_batch_cooldown_until is not None


def test_status_scheduler_is_bounded_and_skips_inactive_tenants():
    due_at = timezone.now() - datetime.timedelta(minutes=1)
    first = _account('bound-first')
    second = _account('bound-second')
    inactive = _account('bound-inactive', tenant_active=False)
    first_listing = _listing(first, 'bound-first', due_at=due_at)
    _listing(second, 'bound-second', due_at=due_at)
    _listing(inactive, 'bound-inactive', due_at=due_at)
    for account in (first, second, inactive):
        _sync_account_due(account)

    with patch('apps.marketplaces.tasks.check_moderation_task.delay') as dispatch:
        result = _dispatch_due_listing_status_checks(
            account_limit=1,
            listing_limit=1,
        )

    assert result['accounts_claimed'] == 1
    assert result['selected'] == 1
    dispatch.assert_called_once_with(first_listing.pk)


def test_broker_failure_releases_batch_without_losing_due_cursor():
    due_at = timezone.now() - datetime.timedelta(minutes=1)
    account = _account('broker')
    _listing(account, 'broker', due_at=due_at)
    _sync_account_due(account)

    with patch(
        'apps.marketplaces.tasks.check_moderation_task.delay',
        side_effect=RuntimeError('broker unavailable'),
    ):
        result = _dispatch_due_listing_status_checks()

    account.refresh_from_db()
    assert result['dispatch_failed'] == 1
    assert result['scheduled'] == 0
    assert account.status_batch_due_at == due_at
    assert account.status_batch_claim_token is None
    assert account.status_batch_claimed_until is None


def test_periodic_entrypoint_does_not_fan_out_future_active_rows():
    checked_at = timezone.now()
    account = _account('entrypoint')
    due = _listing(
        account,
        'entrypoint-due',
        due_at=checked_at - datetime.timedelta(minutes=1),
    )
    _listing(
        account,
        'entrypoint-future',
        due_at=checked_at + datetime.timedelta(hours=2),
    )
    _sync_account_due(account)

    with (
        patch(
            'apps.marketplaces.tasks._repair_orphaned_pending_feed_intents',
            return_value={'selected': 0, 'repaired': 0},
        ),
        patch('apps.marketplaces.tasks.check_moderation_task.delay') as dispatch,
        patch('apps.marketplaces.tasks.poll_feed_results_task.delay'),
        patch('apps.marketplaces.tasks.publish_listing_task.delay'),
    ):
        result = check_moderation_status()

    dispatch.assert_called_once_with(due.pk)
    assert result['active_listings_queued'] == 1
    assert result['status_rows_selected'] == 1


def test_periodic_entrypoint_reports_queued_dispatch_failure_truthfully():
    account = _account('queued-broker')
    listing = _listing(account, 'queued-broker', due_at=None)
    Listing.objects.filter(pk=listing.pk).update(
        status=Listing.STATUS_QUEUED,
        external_id=None,
    )

    with (
        patch(
            'apps.marketplaces.tasks._repair_orphaned_pending_feed_intents',
            return_value={'selected': 0, 'repaired': 0},
        ),
        patch(
            'apps.marketplaces.tasks.publish_listing_task.delay',
            side_effect=RuntimeError('broker unavailable'),
        ),
    ):
        result = check_moderation_status()

    assert result['queued_accounts_started'] == 0
    assert result['periodic_dispatch_failed'] == 1
