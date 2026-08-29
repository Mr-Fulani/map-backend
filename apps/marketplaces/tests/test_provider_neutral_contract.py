from decimal import Decimal
from unittest.mock import patch

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
        'catalog_schema': False,
        'publication_preflight': False,
        'publish_or_update': False,
        'price_update': False,
        'stock_update': False,
        'archive': False,
        'status_reconcile': False,
        'statistics': False,
        'feed_delivery': False,
        'placement_addresses': False,
        'publication': False,
        'status_check': False,
        'analytics': False,
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
def test_product_publish_requires_and_respects_explicit_multi_account_targets():
    tenant, token = _tenant('mutation-publish-targets')
    first = _account(tenant, MarketplaceAccount.MARKETPLACE_AVITO, 'first')
    second = _account(tenant, MarketplaceAccount.MARKETPLACE_AVITO, 'second')
    unselected = _account(tenant, MarketplaceAccount.MARKETPLACE_AVITO, 'unselected')
    product = Product.objects.create(
        tenant=tenant,
        article='ART-TARGETS',
        name='Targeted product',
        price=Decimal('100.00'),
        stock_qty=1,
    )
    client = Client()

    missing = client.post(
        f'/api/v1/products/{product.pk}/publish/',
        {},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )
    response = client.post(
        f'/api/v1/products/{product.pk}/publish/',
        {'account_ids': [second.pk, first.pk]},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )

    assert missing.status_code == 400
    assert response.status_code == 200
    listings = list(Listing.objects.filter(product=product).order_by('account_id'))
    assert [listing.account_id for listing in listings] == [first.pk, second.pk]
    assert all(listing.status == Listing.STATUS_DRAFT for listing in listings)
    assert not Listing.objects.filter(product=product, account=unselected).exists()
    assert set(response.json()['data']['listing_ids']) == {listing.pk for listing in listings}


@pytest.mark.django_db
def test_product_publish_rejects_foreign_or_unsupported_target_atomically():
    tenant, token = _tenant('mutation-publish-fence')
    own = _account(tenant, MarketplaceAccount.MARKETPLACE_AVITO, 'own')
    unsupported = _account(tenant, 'ozon', 'unsupported')
    other, _ = _tenant('mutation-publish-fence-other')
    foreign = _account(other, MarketplaceAccount.MARKETPLACE_AVITO, 'foreign')
    product = Product.objects.create(
        tenant=tenant,
        article='ART-FENCE',
        name='Fenced product',
        price=Decimal('100.00'),
        stock_qty=1,
    )
    client = Client()

    foreign_response = client.post(
        f'/api/v1/products/{product.pk}/publish/',
        {'account_ids': [own.pk, foreign.pk]},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )
    unsupported_response = client.post(
        f'/api/v1/products/{product.pk}/publish/',
        {'account_ids': [unsupported.pk]},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )

    assert foreign_response.status_code == 400
    assert foreign_response.json()['code'] == 'invalid_account_targets'
    assert unsupported_response.status_code == 400
    assert unsupported_response.json()['code'] == 'provider_capability_unavailable'
    assert not Listing.objects.filter(product=product).exists()


@pytest.mark.django_db
def test_product_archive_only_mutates_selected_account_scope(
    settings,
    django_capture_on_commit_callbacks,
):
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'dual_write'
    tenant, token = _tenant('mutation-archive-target')
    selected = _account(tenant, MarketplaceAccount.MARKETPLACE_AVITO, 'selected')
    untouched = _account(tenant, MarketplaceAccount.MARKETPLACE_AVITO, 'untouched')
    selected_listing = _listing(
        tenant,
        selected,
        'selected',
        status=Listing.STATUS_ACTIVE,
    )
    untouched_listing = Listing.objects.create(
        tenant=tenant,
        product=selected_listing.product,
        account=untouched,
        title='Untouched listing',
        price_on_listing=Decimal('100.00'),
        status=Listing.STATUS_ACTIVE,
    )

    with patch('apps.marketplaces.services._enqueue_unpublish') as enqueue, \
         django_capture_on_commit_callbacks(execute=True):
        response = Client().post(
            f'/api/v1/products/{selected_listing.product_id}/archive/',
            {'account_ids': [selected.pk]},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    selected_listing.refresh_from_db()
    untouched_listing.refresh_from_db()
    assert response.status_code == 200
    assert response.json()['data']['archived_count'] == 1
    assert selected_listing.status == Listing.STATUS_ARCHIVING
    assert untouched_listing.status == Listing.STATUS_ACTIVE
    enqueue.assert_called_once_with(selected_listing.pk)


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
