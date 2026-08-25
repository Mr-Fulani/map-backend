import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.db.models.deletion import ProtectedError
from django.test import RequestFactory

from apps.marketplaces.admin import (
    CategoryMappingAdmin,
    ListingAdmin,
    MarketplaceAccountAdmin,
)
from apps.marketplaces.feed_workflow import account_identity_digest
from apps.marketplaces.models import (
    CategoryMapping,
    Listing,
    MarketplaceAccount,
    MarketplaceFeedEndpoint,
)
from apps.tenants.admin import TenantAdmin
from apps.tenants.models import Tenant


@pytest.mark.django_db
def test_marketplace_admin_closes_feed_visible_raw_writers():
    request = RequestFactory().get('/admin/marketplaces/')
    request.user = get_user_model().objects.create_superuser(
        'marketplace-feed-admin-safety@example.com',
        'pass12345',
    )

    account_admin = MarketplaceAccountAdmin(MarketplaceAccount, AdminSite())
    assert {
        field.name for field in MarketplaceAccount._meta.fields
    } == set(account_admin.get_readonly_fields(request))
    assert account_admin.has_add_permission(request) is False
    assert account_admin.has_change_permission(request) is False
    assert account_admin.has_delete_permission(request) is False

    listing_admin = ListingAdmin(Listing, AdminSite())
    assert {
        'title',
        'description_ai',
        'price_on_listing',
        'margin_pct',
    } <= set(listing_admin.get_readonly_fields(request))
    assert listing_admin.has_delete_permission(request) is False
    assert 'delete_selected' not in listing_admin.get_actions(request)

    mapping_admin = CategoryMappingAdmin(CategoryMapping, AdminSite())
    assert mapping_admin.has_add_permission(request) is False
    assert mapping_admin.has_change_permission(request) is False
    assert mapping_admin.has_delete_permission(request) is False
    assert 'delete_selected' not in mapping_admin.get_actions(request)


@pytest.mark.django_db
def test_tenant_admin_and_model_refuse_destructive_feed_owner_cascade():
    request = RequestFactory().get('/admin/tenants/tenant/')
    request.user = get_user_model().objects.create_superuser(
        'tenant-admin-safety@example.com',
        'pass12345',
    )
    tenant_admin = TenantAdmin(Tenant, AdminSite())
    assert 'is_active' in tenant_admin.get_readonly_fields(request)
    assert tenant_admin.has_delete_permission(request) is False
    assert 'delete_selected' not in tenant_admin.get_actions(request)

    tenant = Tenant.objects.create(name='Protected tenant', slug='protected-tenant')
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        name='Protected Avito',
        external_id='protected-avito',
        credentials_enc=b'opaque-test-credentials',
    )
    endpoint = MarketplaceFeedEndpoint.objects.create(
        account=account,
        token_key_id='feed-v1',
        owner_identity_digest=account_identity_digest(account),
        profile_state=MarketplaceFeedEndpoint.ProfileState.MANUAL_REVIEW,
    )

    with pytest.raises(ProtectedError):
        tenant.delete()

    assert Tenant.objects.filter(pk=tenant.pk).exists()
    assert MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).exists()
