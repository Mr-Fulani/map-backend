from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from apps.datasources.encryption import encrypt
from apps.marketplaces.models import Listing, ListingStats, MarketplaceAccount
from apps.products.models import Product
from apps.tenants.services import TenantService
from apps.tenants.tests.auth import owner_access_token


def _tenant(slug: str):
    tenant, _ = TenantService.create_tenant(
        slug, slug, f'{slug}@test.com', 'pass12345',
    )
    return tenant, owner_access_token(tenant)


def _account(tenant, marketplace: str, suffix: str) -> MarketplaceAccount:
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        name=f'{marketplace.title()} {suffix}',
        marketplace=marketplace,
        external_id=f'{marketplace}-{tenant.pk}-{suffix}',
        credentials_enc=encrypt({'test': suffix}),
    )


def _listing(tenant, account, suffix: str, *, status=Listing.STATUS_DRAFT):
    product = Product.objects.create(
        tenant=tenant,
        article=f'ART-{suffix}',
        name=f'Product {suffix}',
        price=Decimal('100.00'),
        stock_qty=1,
    )
    return Listing.objects.create(
        tenant=tenant,
        product=product,
        account=account,
        title=product.name,
        price_on_listing=product.price,
        status=status,
    )


@pytest.mark.django_db
def test_account_read_contract_is_provider_neutral_and_fail_closed():
    tenant, token = _tenant('provider-account')
    _account(tenant, MarketplaceAccount.MARKETPLACE_AVITO, 'a')
    ozon = _account(tenant, 'ozon', 'o')
    other, _ = _tenant('provider-account-other')
    _account(other, 'ozon', 'foreign')

    response = Client().get(
        '/api/v1/accounts/',
        {'marketplace': 'ozon'},
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )

    assert response.status_code == 200
    assert [row['id'] for row in response.json()] == [ozon.pk]
    account = response.json()[0]
    assert account['marketplace'] == 'ozon'
    assert account['marketplace_label'] == 'Ozon'
    assert account['provider_capabilities'] == {
        'account_health': False,
        'publication': False,
        'status_check': False,
        'analytics': False,
        'feed_delivery': False,
        'placement_addresses': False,
    }
    assert account['avito_status'] is None
    assert account['autoload_onboarding'] is None
    assert account['feed_endpoint_managed'] is False


@pytest.mark.django_db
def test_account_filter_rejects_malformed_marketplace():
    tenant, token = _tenant('provider-account-invalid')
    _account(tenant, MarketplaceAccount.MARKETPLACE_AVITO, 'a')

    response = Client().get(
        '/api/v1/accounts/',
        {'marketplace': 'ozon!'},
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )

    assert response.status_code == 400
    assert 'marketplace' in response.json()['errors']

    account_response = Client().get(
        '/api/v1/listings/',
        {'account': '-1'},
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )
    assert account_response.status_code == 400
    assert 'account' in account_response.json()['errors']


@pytest.mark.django_db
def test_listing_filters_partition_marketplaces_and_accounts_inside_tenant():
    tenant, token = _tenant('provider-listing')
    avito = _account(tenant, MarketplaceAccount.MARKETPLACE_AVITO, 'a')
    ozon = _account(tenant, 'ozon', 'o')
    _listing(tenant, avito, 'a')
    ozon_listing = _listing(tenant, ozon, 'o')
    other, _ = _tenant('provider-listing-other')
    foreign = _account(other, 'ozon', 'foreign')
    _listing(other, foreign, 'foreign')

    response = Client().get(
        '/api/v1/listings/',
        {'marketplace': 'ozon', 'account': ozon.pk},
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )

    assert response.status_code == 200
    assert response.json()['meta']['total'] == 1
    listing = response.json()['data'][0]
    assert listing['id'] == ozon_listing.pk
    assert listing['marketplace'] == 'ozon'
    assert listing['marketplace_label'] == 'Ozon'
    assert listing['can_publish'] is False
    assert listing['can_check_provider_status'] is False
    assert listing['lifecycle_actions_blocked'] is True
    assert listing['status_explanation'] == ''

    foreign_response = Client().get(
        '/api/v1/listings/',
        {'account': foreign.pk},
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )
    assert foreign_response.status_code == 200
    assert foreign_response.json()['meta']['total'] == 0


@pytest.mark.django_db
def test_analytics_filters_by_marketplace_and_account_without_cross_tenant_leak():
    tenant, token = _tenant('provider-analytics')
    avito = _account(tenant, MarketplaceAccount.MARKETPLACE_AVITO, 'a')
    ozon = _account(tenant, 'ozon', 'o')
    avito_listing = _listing(tenant, avito, 'a', status=Listing.STATUS_ACTIVE)
    ozon_listing = _listing(tenant, ozon, 'o', status=Listing.STATUS_ACTIVE)
    today = timezone.localdate()
    ListingStats.objects.create(
        tenant=tenant, listing=avito_listing, date=today,
        views=10, contacts=2, impressions=20,
    )
    ListingStats.objects.create(
        tenant=tenant, listing=ozon_listing, date=today,
        views=7, contacts=1, impressions=14,
    )
    other, _ = _tenant('provider-analytics-other')
    foreign = _account(other, 'ozon', 'foreign')
    _listing(other, foreign, 'foreign', status=Listing.STATUS_ACTIVE)
    _listing(tenant, foreign, 'inconsistent', status=Listing.STATUS_ACTIVE)

    response = Client().get(
        '/api/v1/analytics/',
        {
            'date_from': today.isoformat(),
            'date_to': today.isoformat(),
            'marketplace': 'ozon',
            'account': ozon.pk,
        },
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )

    assert response.status_code == 200
    summary = response.json()['data']['summary']
    assert summary['views'] == 7
    assert summary['contacts'] == 1
    assert summary['impressions'] == 14
    assert summary['active_listings'] == 1

    foreign_response = Client().get(
        '/api/v1/analytics/',
        {
            'date_from': today.isoformat(),
            'date_to': today.isoformat(),
            'account': foreign.pk,
        },
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )
    assert foreign_response.status_code == 200
    assert foreign_response.json()['data']['summary']['views'] == 0
    assert foreign_response.json()['data']['summary']['active_listings'] == 0
