from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.datasources.encryption import encrypt
from apps.marketplaces.feed_workflow import FeedRunConflict
from apps.marketplaces.models import Listing, MarketplaceAccount, MarketplaceFeedRun
from apps.marketplaces.tasks import (
    _MAX_DURABLE_FEED_PAYLOAD_LISTINGS,
    _account_feed_listings,
    _coalesced_flush_durable,
    coalesced_flush_task,
)
from apps.products.models import Product
from apps.sync.models import SyncLog
from apps.tenants.models import Tenant


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _durable_mode(settings):
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'dual_write'
    settings.MARKETPLACE_FEED_RUN_MODE = 'durable'


def _account(suffix: str) -> MarketplaceAccount:
    tenant = Tenant.objects.create(
        name=f'Payload limit {suffix}',
        slug=f'payload-limit-{suffix}',
    )
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Avito {suffix}',
        external_id=f'avito-payload-{suffix}',
        credentials_enc=encrypt({'client_id': 'cid', 'client_secret': 'secret'}),
    )


def _listing(
    account: MarketplaceAccount,
    suffix: str,
    *,
    status: str = Listing.STATUS_PENDING,
) -> Listing:
    product = Product.objects.create(
        tenant_id=account.tenant_id,
        article=f'PAYLOAD-{account.pk}-{suffix}',
        name=f'Payload product {suffix}',
        price=Decimal('1000.00'),
    )
    return Listing.objects.create(
        tenant_id=account.tenant_id,
        account=account,
        product=product,
        status=status,
        price_on_listing=Decimal('1100.00'),
    )


def test_account_feed_listing_probe_applies_database_limit():
    account = _account('query-limit')
    created = [
        _listing(account, 'one', status=Listing.STATUS_ACTIVE),
        _listing(account, 'two', status=Listing.STATUS_PENDING),
        _listing(account, 'three', status=Listing.STATUS_QUEUED),
        _listing(account, 'four', status=Listing.STATUS_ACTIVE),
    ]

    probed = _account_feed_listings(account, limit=3)

    assert [listing.pk for listing in probed] == [listing.pk for listing in created[:3]]


def test_durable_payload_over_limit_fails_before_xml_storage_and_provider_post():
    account = _account('over-limit')
    capped_probe = [object()] * (_MAX_DURABLE_FEED_PAYLOAD_LISTINGS + 1)

    with (
        patch(
            'apps.marketplaces.tasks._account_feed_listings',
            return_value=capped_probe,
        ) as feed_query,
        patch('apps.marketplaces.tasks._feed_payload_bytes') as build_payload,
        patch('apps.marketplaces.tasks.create_or_supersede_feed_run') as create_run,
        patch('apps.marketplaces.tasks.AvitoAdapter') as adapter,
    ):
        result = _coalesced_flush_durable(None, account)

    queried_account = feed_query.call_args.args[0]
    assert queried_account.pk == account.pk
    assert feed_query.call_args.kwargs == {
        'limit': _MAX_DURABLE_FEED_PAYLOAD_LISTINGS + 1,
    }
    assert result == {
        'status': 'payload_limit_exceeded',
        'limit': _MAX_DURABLE_FEED_PAYLOAD_LISTINGS,
        'observed_at_least': _MAX_DURABLE_FEED_PAYLOAD_LISTINGS + 1,
    }
    build_payload.assert_not_called()
    create_run.assert_not_called()
    adapter.assert_not_called()
    assert not MarketplaceFeedRun.objects.filter(account=account).exists()

    log = SyncLog.objects.get(tenant_id=account.tenant_id)
    assert log.event_type == SyncLog.EVENT_LISTING_ERROR
    assert log.status == SyncLog.STATUS_ERROR
    assert str(_MAX_DURABLE_FEED_PAYLOAD_LISTINGS) in log.message
    assert len(log.message) < 300


def test_durable_payload_at_exact_limit_is_not_rejected():
    account = _account('exact-limit')
    exact_probe = [object()] * _MAX_DURABLE_FEED_PAYLOAD_LISTINGS

    with (
        patch(
            'apps.marketplaces.tasks._account_feed_listings',
            return_value=exact_probe,
        ),
        patch('apps.marketplaces.tasks._feed_payload_bytes', return_value=b'feed') as build_payload,
        patch(
            'apps.marketplaces.tasks.create_or_supersede_feed_run',
            side_effect=FeedRunConflict('existing owner'),
        ),
        patch('apps.marketplaces.tasks._reschedule_coalesced_flush_after_conflict') as reschedule,
    ):
        result = _coalesced_flush_durable(None, account)

    assert result == {'status': 'active_feed_run'}
    build_payload.assert_called_once_with(exact_probe)
    reschedule.assert_called_once_with(account.pk)
    assert not SyncLog.objects.filter(tenant_id=account.tenant_id).exists()


def test_over_limit_wins_over_inactive_autoload_without_listing_fan_out():
    account = _account('inactive-autoload-over-limit')
    _listing(account, 'pending')
    capped_probe = [object()] * (_MAX_DURABLE_FEED_PAYLOAD_LISTINGS + 1)

    with (
        patch('apps.marketplaces.tasks.cache.delete'),
        patch('apps.marketplaces.tasks._feed_window_remaining', return_value=0),
        patch('apps.marketplaces.tasks._promote_queued_feed_rows'),
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.is_autoload_active',
            return_value=False,
        ),
        patch(
            'apps.marketplaces.tasks._account_feed_listings',
            return_value=capped_probe,
        ) as feed_query,
        patch('apps.marketplaces.tasks._reject_listing') as reject_listing,
        patch('apps.marketplaces.tasks._coalesced_flush_durable') as durable_owner,
    ):
        result = coalesced_flush_task(account.pk)

    assert result == {
        'status': 'payload_limit_exceeded',
        'limit': _MAX_DURABLE_FEED_PAYLOAD_LISTINGS,
        'observed_at_least': _MAX_DURABLE_FEED_PAYLOAD_LISTINGS + 1,
    }
    assert feed_query.call_args.kwargs == {
        'limit': _MAX_DURABLE_FEED_PAYLOAD_LISTINGS + 1,
    }
    reject_listing.assert_not_called()
    durable_owner.assert_not_called()
    assert SyncLog.objects.filter(
        tenant_id=account.tenant_id,
        event_type=SyncLog.EVENT_LISTING_ERROR,
        status=SyncLog.STATUS_ERROR,
    ).count() == 1


def test_coalesced_durable_path_does_not_materialize_full_payload_twice():
    account = _account('single-materialization')
    _listing(account, 'pending')
    durable_result = {'status': 'durable-owner'}

    with (
        patch('apps.marketplaces.tasks.cache.delete'),
        patch('apps.marketplaces.tasks._feed_window_remaining', return_value=0),
        patch('apps.marketplaces.tasks._promote_queued_feed_rows'),
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.is_autoload_active',
            return_value=True,
        ),
        patch(
            'apps.marketplaces.tasks._coalesced_flush_durable',
            return_value=durable_result,
        ) as durable_owner,
        patch('apps.marketplaces.tasks._account_feed_listings') as full_materialization,
    ):
        result = coalesced_flush_task(account.pk)

    assert result == durable_result
    full_materialization.assert_not_called()
    assert durable_owner.call_count == 1
    assert durable_owner.call_args.args[1].pk == account.pk
