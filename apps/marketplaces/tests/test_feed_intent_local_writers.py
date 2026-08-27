from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.marketplaces.models import (
    CategoryMapping,
    Listing,
    MarketplaceAccount,
    MarketplacePlacementAddress,
)
from apps.marketplaces.services import (
    CategoryMappingService,
    ListingService,
    MarketplaceAccountService,
    MarketplaceAccountFeedConflict,
    MarketplacePlacementAddressService,
    _listing_expected_state,
    _save_local_listing_intent as save_service_intent,
)
from apps.marketplaces.tasks import (
    _account_feed_listings,
    _promote_queued_feed_rows,
    _reject_listing,
    _repair_orphaned_pending_feed_intents,
    _save_local_listing_intent as save_task_intent,
    confirm_removal_task,
    publish_listing_task,
    unpublish_listing_task,
)
from apps.products.models import Product
from apps.sync.models import SyncLog
from apps.tenants.models import Tenant


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _dual_write_modes(settings):
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'dual_write'
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'dual_write'


def _account(tenant: Tenant, suffix: str) -> MarketplaceAccount:
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Account {suffix}',
        external_id=f'account-{suffix}-{uuid4().hex[:8]}',
        credentials_enc=b'opaque-test-credentials',
        default_manager_name='Manager',
        default_contact_phone='+79990000000',
    )


def _listing(
    *,
    status: str = Listing.STATUS_ACTIVE,
    account: MarketplaceAccount | None = None,
    suffix: str | None = None,
) -> tuple[Tenant, MarketplaceAccount, Product, Listing]:
    suffix = suffix or uuid4().hex[:10]
    if account is None:
        tenant = Tenant.objects.create(
            name=f'Tenant {suffix}',
            slug=f'feed-writer-{suffix}',
        )
        account = _account(tenant, suffix)
    else:
        tenant = account.tenant
    product = Product.objects.create(
        tenant=tenant,
        article=f'ARTICLE-{suffix}',
        name=f'Product {suffix}',
        brand='Brand',
        price=Decimal('1000.00'),
        stock_qty=2,
    )
    external_id = f'remote-{suffix}' if status == Listing.STATUS_ACTIVE else None
    listing = Listing.objects.create(
        tenant=tenant,
        account=account,
        product=product,
        status=status,
        external_id=external_id,
        price_on_listing=Decimal('1100.00'),
    )
    return tenant, account, product, listing


def test_legacy_listing_writer_does_not_touch_feed_intent(settings):
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'legacy'
    tenant, account, _product, listing = _listing()

    with patch('apps.marketplaces.services.transaction.on_commit'):
        ListingService.archive(listing.pk, tenant)

    account.refresh_from_db()
    assert account.feed_intent_revision == 0
    assert account.feed_intent_due_at is None


@pytest.mark.parametrize('method_name', ('archive', 'delete'))
def test_live_local_removal_advances_exactly_one_intent(method_name):
    tenant, account, _product, listing = _listing()

    with patch('apps.marketplaces.services.transaction.on_commit'):
        getattr(ListingService, method_name)(listing.pk, tenant)

    account.refresh_from_db()
    assert account.feed_intent_revision == 1
    assert account.feed_intent_due_at is not None


def test_validated_queued_to_pending_task_advances_exactly_once():
    _tenant, account, _product, listing = _listing(
        status=Listing.STATUS_QUEUED,
    )
    listing.status = Listing.STATUS_PENDING

    assert save_task_intent(
        listing,
        update_fields=('status',),
        expected_status=Listing.STATUS_QUEUED,
        expected_external_id=None,
        feed_projection_changed=True,
    )

    listing.refresh_from_db()
    account.refresh_from_db()
    assert listing.status == Listing.STATUS_PENDING
    assert account.feed_intent_revision == 1

    # A delayed duplicate sees no desired-state change and cannot double bump.
    assert save_task_intent(
        listing,
        update_fields=('status',),
        expected_status=Listing.STATUS_PENDING,
        expected_external_id=None,
        feed_projection_changed=True,
    )
    account.refresh_from_db()
    assert account.feed_intent_revision == 1


def test_real_publish_task_advances_feed_intent_in_dual_write():
    _tenant, account, _product, listing = _listing(
        status=Listing.STATUS_QUEUED,
    )

    with patch(
        'apps.marketplaces.tasks._validate_feed_batch',
        side_effect=lambda rows: rows,
    ), patch(
        'apps.marketplaces.tasks.LimitChecker.can_publish',
        return_value=(True, ''),
    ), patch('apps.marketplaces.tasks.request_feed_flush') as request_flush:
        publish_listing_task(listing.pk)

    listing.refresh_from_db()
    account.refresh_from_db()
    assert listing.status == Listing.STATUS_PENDING
    assert account.feed_intent_revision == 1
    assert account.feed_intent_due_at is not None
    request_flush.assert_called_once()


def test_direct_unpublish_task_advances_feed_intent_in_dual_write():
    _tenant, account, _product, listing = _listing(
        status=Listing.STATUS_ACTIVE,
    )

    with patch('apps.marketplaces.tasks.request_feed_flush') as request_flush:
        unpublish_listing_task(listing.pk)

    listing.refresh_from_db()
    account.refresh_from_db()
    assert listing.status == Listing.STATUS_ARCHIVING
    assert account.feed_intent_revision == 1
    assert account.feed_intent_due_at is not None
    request_flush.assert_called_once()


def test_orphan_pending_repair_requires_post_flush_publish_evidence(settings):
    settings.MARKETPLACE_FEED_RUN_MODE = 'durable'
    _tenant, account, _product, listing = _listing(
        status=Listing.STATUS_PENDING,
    )
    flushed_at = timezone.now() - timedelta(hours=2)
    MarketplaceAccount.objects.filter(pk=account.pk).update(
        last_feed_flush_at=flushed_at,
    )
    evidence = SyncLog.objects.create(
        tenant=account.tenant,
        listing=listing,
        event_type=SyncLog.EVENT_LISTING_PUBLISH,
        status=SyncLog.STATUS_OK,
        message='Accepted locally but no feed revision was created.',
    )
    SyncLog.objects.filter(pk=evidence.pk).update(
        created_at=timezone.now() - timedelta(minutes=10),
    )

    first = _repair_orphaned_pending_feed_intents()
    second = _repair_orphaned_pending_feed_intents()

    account.refresh_from_db()
    assert first == {'selected': 1, 'repaired': 1}
    assert second == {'selected': 0, 'repaired': 0}
    assert account.feed_intent_revision == 1
    assert account.feed_intent_due_at is not None
    assert SyncLog.objects.filter(
        listing=listing,
        payload__repair='orphan_pending_feed_intent',
    ).count() == 1


def test_orphan_pending_repair_does_not_replay_a_post_flush_listing(settings):
    settings.MARKETPLACE_FEED_RUN_MODE = 'durable'
    _tenant, account, _product, listing = _listing(
        status=Listing.STATUS_PENDING,
    )
    evidence = SyncLog.objects.create(
        tenant=account.tenant,
        listing=listing,
        event_type=SyncLog.EVENT_LISTING_PUBLISH,
        status=SyncLog.STATUS_OK,
        message='Normal publication evidence.',
    )
    evidence_at = timezone.now() - timedelta(hours=2)
    SyncLog.objects.filter(pk=evidence.pk).update(created_at=evidence_at)
    MarketplaceAccount.objects.filter(pk=account.pk).update(
        last_feed_flush_at=evidence_at + timedelta(minutes=1),
    )

    result = _repair_orphaned_pending_feed_intents()

    account.refresh_from_db()
    assert result == {'selected': 0, 'repaired': 0}
    assert account.feed_intent_revision == 0


def test_dual_write_projection_excludes_and_never_bulk_promotes_queued():
    _tenant, account, _product, queued = _listing(
        status=Listing.STATUS_QUEUED,
    )
    _listing(status=Listing.STATUS_PENDING, account=account)

    projected = _account_feed_listings(account)

    assert [row.status for row in projected] == [Listing.STATUS_PENDING]
    assert _promote_queued_feed_rows(account) == 0
    queued.refresh_from_db()
    assert queued.status == Listing.STATUS_QUEUED


def test_legacy_projection_keeps_queued_compatibility(settings):
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'legacy'
    _tenant, account, _product, queued = _listing(
        status=Listing.STATUS_QUEUED,
    )

    assert [row.pk for row in _account_feed_listings(account)] == [queued.pk]
    assert _promote_queued_feed_rows(account) == 1
    queued.refresh_from_db()
    assert queued.status == Listing.STATUS_PENDING


def test_pending_content_change_bumps_but_stale_generation_does_not():
    tenant, account, _product, listing = _listing(
        status=Listing.STATUS_PENDING,
    )

    ListingService.update_placement(
        listing.pk,
        tenant,
        {'address_override': 'Istanbul, Istiklal Cd. 1'},
    )
    account.refresh_from_db()
    assert account.feed_intent_revision == 1

    stale = Listing.objects.get(pk=listing.pk)
    expected = _listing_expected_state(stale)
    stale.title = 'stale title'
    Listing.objects.filter(pk=listing.pk).update(status=Listing.STATUS_REJECTED)

    assert not save_service_intent(stale, ('title',), **expected)
    account.refresh_from_db()
    assert account.feed_intent_revision == 1


def test_feed_revision_rolls_back_with_listing_domain_failure():
    _tenant, account, _product, listing = _listing(
        status=Listing.STATUS_PENDING,
    )
    expected = _listing_expected_state(listing)
    listing.title = 'new desired title'

    with patch.object(
        Listing,
        'save',
        side_effect=RuntimeError('synthetic listing write failure'),
    ), pytest.raises(RuntimeError, match='synthetic listing write failure'):
        save_service_intent(listing, ('title',), **expected)

    listing.refresh_from_db()
    account.refresh_from_db()
    assert listing.title != 'new desired title'
    assert account.feed_intent_revision == 0
    assert account.feed_intent_due_at is None


def test_pending_account_move_bumps_old_and_new_accounts_once():
    tenant, old_account, _product, listing = _listing(
        status=Listing.STATUS_PENDING,
    )
    new_account = _account(tenant, 'new-target')

    ListingService.update_listing_fields(
        listing.pk,
        tenant,
        {'account_id': new_account.pk},
    )

    listing.refresh_from_db()
    old_account.refresh_from_db()
    new_account.refresh_from_db()
    assert listing.account_id == new_account.pk
    assert old_account.feed_intent_revision == 1
    assert new_account.feed_intent_revision == 1


def test_bulk_placement_advances_once_per_affected_account():
    tenant, first_account, _product, first = _listing()
    _tenant, _account_same, _product, second = _listing(
        account=first_account,
    )
    second_account = _account(tenant, 'bulk-second')
    _tenant, _account_second, _product, third = _listing(
        account=second_account,
    )

    updated = ListingService.bulk_update_placement(
        tenant,
        {'listing_ids': [first.pk, second.pk, third.pk]},
        {'address_override': 'Bulk desired address'},
    )

    first_account.refresh_from_db()
    second_account.refresh_from_db()
    assert updated == 3
    assert first_account.feed_intent_revision == 1
    assert second_account.feed_intent_revision == 1


def test_account_defaults_bump_live_projection_but_name_does_not():
    _tenant, account, _product, _listing_row = _listing()

    MarketplaceAccountService.update_partial(account, {'name': 'Renamed'})
    account.refresh_from_db()
    assert account.feed_intent_revision == 0

    MarketplaceAccountService.update_partial(
        account,
        {'default_address': 'Desired account address'},
    )
    account.refresh_from_db()
    assert account.feed_intent_revision == 1

    # A semantically identical PATCH is not a new desired feed state.
    MarketplaceAccountService.update_partial(
        account,
        {'default_address': 'Desired account address'},
    )
    account.refresh_from_db()
    assert account.feed_intent_revision == 1


def test_provider_status_outcome_does_not_advance_feed_intent():
    _tenant, account, _product, listing = _listing(
        status=Listing.STATUS_PENDING,
    )
    listing.status = Listing.STATUS_ACTIVE
    listing.external_id = 'provider-observation'

    assert save_task_intent(
        listing,
        update_fields=('status', 'external_id'),
        expected_status=Listing.STATUS_PENDING,
        expected_external_id=None,
    )

    account.refresh_from_db()
    assert account.feed_intent_revision == 0


def test_bump_preserves_uncertain_hold_and_account_updated_at():
    tenant, account, _product, listing = _listing(
        status=Listing.STATUS_PENDING,
    )
    MarketplaceAccount.objects.filter(pk=account.pk).update(
        feed_intent_revision=1,
        feed_intent_dispatched_revision=0,
        feed_intent_due_at=None,
    )
    account.refresh_from_db()
    original_updated_at = account.updated_at

    ListingService.update_placement(
        listing.pk,
        tenant,
        {'manager_name_override': 'New Manager'},
    )

    account.refresh_from_db()
    assert account.feed_intent_revision == 2
    assert account.feed_intent_due_at is None
    assert account.updated_at == original_updated_at


def test_deleted_product_cannot_enter_projection_or_leave_feed_revision():
    _tenant, account, product, listing = _listing(
        status=Listing.STATUS_QUEUED,
    )
    product.soft_delete()
    listing.status = Listing.STATUS_PENDING

    assert not save_task_intent(
        listing,
        update_fields=('status',),
        expected_status=Listing.STATUS_QUEUED,
        expected_external_id=None,
        feed_projection_changed=True,
    )

    listing.refresh_from_db()
    account.refresh_from_db()
    assert listing.status == Listing.STATUS_QUEUED
    assert account.feed_intent_revision == 0


def test_local_pre_provider_rejection_creates_successor_but_provider_default_does_not():
    _tenant, account, _product, local_reject = _listing(
        status=Listing.STATUS_PENDING,
    )
    _reject_listing(
        local_reject,
        'definitive local preflight failure',
        feed_projection_changed=True,
    )
    account.refresh_from_db()
    assert account.feed_intent_revision == 1

    _tenant, _same_account, _product, provider_reject = _listing(
        status=Listing.STATUS_PENDING,
        account=account,
    )
    _reject_listing(provider_reject, 'provider item result')
    account.refresh_from_db()
    assert account.feed_intent_revision == 1


def test_account_identity_rotation_advances_live_projection_once():
    _tenant, account, _product, _listing_row = _listing()

    with patch.object(
        MarketplaceAccountService,
        '_fetch_avito_user_id',
        return_value='rotated-provider-account',
    ):
        MarketplaceAccountService.update_credentials(account, {
            'name': 'Rotated account',
            'marketplace': MarketplaceAccount.MARKETPLACE_AVITO,
            'client_id': 'client',
            'client_secret': 'secret',
        })

    account.refresh_from_db()
    assert account.feed_intent_revision == 1


def test_account_reactivation_reconciles_changes_missed_while_paused():
    _tenant, account, product, _listing_row = _listing()
    MarketplaceAccount.objects.filter(pk=account.pk).update(is_active=False)
    account.refresh_from_db()
    product.name = 'Changed while account was paused'
    product.save(update_fields=['name', 'updated_at'])

    MarketplaceAccountService.update_partial(account, {'is_active': True})

    account.refresh_from_db()
    assert account.is_active is True
    assert account.feed_intent_revision == 1


@pytest.mark.parametrize(
    ('status', 'external_id'),
    (
        (Listing.STATUS_ACTIVE, None),
        (Listing.STATUS_PENDING, None),
        (Listing.STATUS_QUEUED, None),
        (Listing.STATUS_ARCHIVING, None),
        (Listing.STATUS_REJECTED, 'stale-rejected-provider-id'),
        (Listing.STATUS_DELETED, 'stale-deleted-provider-id'),
    ),
)
def test_account_delete_fails_closed_for_live_or_stale_provider_ownership(
    status,
    external_id,
):
    _tenant, account, _product, listing = _listing(status=status)
    Listing.objects.filter(pk=listing.pk).update(external_id=external_id)

    with pytest.raises(MarketplaceAccountFeedConflict):
        account.soft_delete()

    account.refresh_from_db()
    listing.refresh_from_db()
    assert account.deleted_at is None
    assert account.is_active is True
    assert listing.deleted_at is None


def test_confirmed_archived_provider_tombstone_allows_account_delete():
    _tenant, account, _product, listing = _listing(
        status=Listing.STATUS_ARCHIVING,
    )
    Listing.objects.filter(pk=listing.pk).update(
        external_id='confirmed-archived-provider-id',
    )

    with patch(
        'apps.marketplaces.tasks.AvitoAdapter.get_status',
        return_value={'status': 'old'},
    ):
        result = confirm_removal_task(listing.pk)

    listing.refresh_from_db()
    assert result == {'status': 'archived', 'changed': True}
    assert listing.status == Listing.STATUS_ARCHIVED
    assert listing.external_id == 'confirmed-archived-provider-id'

    account.soft_delete()

    account.refresh_from_db()
    listing.refresh_from_db()
    assert account.deleted_at is not None
    assert account.is_active is False
    assert listing.deleted_at == account.deleted_at


def test_placement_address_create_default_switch_update_and_delete_bump_once():
    tenant, account, _product, _listing_row = _listing()
    previous = MarketplacePlacementAddress.objects.create(
        tenant=tenant,
        account=account,
        name='Previous',
        address='Old address',
        is_default=True,
    )

    created = MarketplacePlacementAddressService.create(tenant, {
        'account': account,
        'name': 'Created',
        'address': 'New address',
        'is_default': True,
        'is_active': True,
    })
    previous.refresh_from_db()
    account.refresh_from_db()
    assert previous.is_default is False
    assert created.is_default is True
    assert account.feed_intent_revision == 1

    MarketplacePlacementAddressService.update(
        created,
        tenant,
        {'contact_phone': '+79991112233'},
    )
    account.refresh_from_db()
    assert account.feed_intent_revision == 2

    MarketplacePlacementAddressService.deactivate(created, tenant)
    created.refresh_from_db()
    account.refresh_from_db()
    assert created.is_active is False
    assert account.feed_intent_revision == 3


def test_reselecting_duplicate_default_address_records_peer_demotion():
    tenant, account, _product, _listing_row = _listing()
    stale_default = MarketplacePlacementAddress.objects.create(
        tenant=tenant,
        account=account,
        name='Legacy first default',
        address='First address',
        is_default=True,
    )
    selected = MarketplacePlacementAddress.objects.create(
        tenant=tenant,
        account=account,
        name='Legacy second default',
        address='Second address',
        is_default=True,
    )

    MarketplacePlacementAddressService.update(
        selected,
        tenant,
        {'is_default': True},
    )

    stale_default.refresh_from_db()
    selected.refresh_from_db()
    account.refresh_from_db()
    assert stale_default.is_default is False
    assert selected.is_default is True
    assert account.feed_intent_revision == 1


def test_placement_address_account_move_bumps_old_and_new_once():
    tenant, old_account, _product, _listing_row = _listing()
    new_account = _account(tenant, 'address-move')
    _listing(account=new_account)
    address = MarketplacePlacementAddress.objects.create(
        tenant=tenant,
        account=old_account,
        name='Movable',
        address='Before move',
    )

    moved = MarketplacePlacementAddressService.update(
        address,
        tenant,
        {'account': new_account, 'address': 'After move'},
    )

    old_account.refresh_from_db()
    new_account.refresh_from_db()
    assert moved.account_id == new_account.pk
    assert old_account.feed_intent_revision == 1
    assert new_account.feed_intent_revision == 1


def test_stale_placement_address_owner_cannot_miss_actual_old_account():
    tenant, old_account, _product, _listing_row = _listing()
    actual_account = _account(tenant, 'address-actual')
    target_account = _account(tenant, 'address-target')
    address = MarketplacePlacementAddress.objects.create(
        tenant=tenant,
        account=old_account,
        name='Stale owner',
    )
    MarketplacePlacementAddress.objects.filter(pk=address.pk).update(
        account=actual_account,
    )

    with pytest.raises(MarketplacePlacementAddress.DoesNotExist):
        MarketplacePlacementAddressService.update(
            address,
            tenant,
            {'account': target_account, 'address': 'Must not apply'},
        )

    address.refresh_from_db()
    old_account.refresh_from_db()
    actual_account.refresh_from_db()
    target_account.refresh_from_db()
    assert address.account_id == actual_account.pk
    assert old_account.feed_intent_revision == 0
    assert actual_account.feed_intent_revision == 0
    assert target_account.feed_intent_revision == 0


def test_placement_address_failure_rolls_back_feed_revision():
    tenant, account, _product, _listing_row = _listing()

    with patch.object(
        MarketplacePlacementAddress.objects,
        'create',
        side_effect=RuntimeError('synthetic address failure'),
    ), pytest.raises(RuntimeError, match='synthetic address failure'):
        MarketplacePlacementAddressService.create(tenant, {
            'account': account,
            'name': 'Failed',
            'is_default': True,
        })

    account.refresh_from_db()
    assert account.feed_intent_revision == 0


def _mapping_payload(source: str, target: str = 'Target') -> dict:
    return {
        source: {
            'category_target': target,
            'category_id': 11,
            'attributes_map': {'source': 'target'},
        },
    }


def test_category_mapping_bulk_bumps_each_live_account_once_and_is_idempotent():
    tenant, first_account, _product, _listing_row = _listing()
    second_account = _account(tenant, 'mapping-second')
    _listing(account=second_account)

    result = CategoryMappingService.bulk_create_from_dict(tenant, {
        **_mapping_payload('One'),
        **_mapping_payload('Two'),
    })

    first_account.refresh_from_db()
    second_account.refresh_from_db()
    assert len(result) == 2
    assert first_account.feed_intent_revision == 1
    assert second_account.feed_intent_revision == 1

    CategoryMappingService.bulk_create_from_dict(tenant, {
        **_mapping_payload('One'),
        **_mapping_payload('Two'),
    })
    first_account.refresh_from_db()
    second_account.refresh_from_db()
    assert first_account.feed_intent_revision == 1
    assert second_account.feed_intent_revision == 1


def test_category_mapping_update_and_delete_each_create_successor():
    tenant, account, _product, _listing_row = _listing()
    mapping = CategoryMapping.objects.create(
        tenant=tenant,
        marketplace=CategoryMapping.MARKETPLACE_AVITO,
        category_source='Source',
        category_target='Before',
        category_id=1,
    )

    mapping = CategoryMappingService.update(mapping, {
        'category_source': 'Source',
        'category_target': 'After',
        'category_id': 2,
        'attributes_map': {},
    })
    account.refresh_from_db()
    assert mapping.version == 2
    assert account.feed_intent_revision == 1

    assert CategoryMappingService.delete(tenant, mapping.pk) == 1
    account.refresh_from_db()
    assert account.feed_intent_revision == 2


def test_category_mapping_failure_rolls_back_all_feed_revisions():
    tenant, account, _product, _listing_row = _listing()

    with patch.object(
        CategoryMapping.objects,
        'update_or_create',
        side_effect=RuntimeError('synthetic mapping failure'),
    ), pytest.raises(RuntimeError, match='synthetic mapping failure'):
        CategoryMappingService.bulk_create_from_dict(
            tenant,
            _mapping_payload('Failed'),
        )

    account.refresh_from_db()
    assert account.feed_intent_revision == 0
    assert CategoryMapping.objects.count() == 0


def test_mapping_change_records_paused_account_successor_for_reactivation():
    tenant, account, _product, _listing_row = _listing()
    MarketplaceAccount.objects.filter(pk=account.pk).update(is_active=False)

    CategoryMappingService.bulk_create_from_dict(
        tenant,
        _mapping_payload('Paused'),
    )

    account.refresh_from_db()
    assert account.feed_intent_revision == 1
    assert account.is_active is False


def test_mapping_change_records_successor_while_tenant_is_paused():
    tenant, account, _product, _listing_row = _listing()
    Tenant.objects.filter(pk=tenant.pk).update(is_active=False)

    CategoryMappingService.bulk_create_from_dict(
        tenant,
        _mapping_payload('Paused tenant'),
    )

    account.refresh_from_db()
    assert account.feed_intent_revision == 1
