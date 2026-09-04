from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.datasources.encryption import encrypt
from apps.marketplaces.models import Listing, MarketplaceAccount, OzonOfferDraft
from apps.products.models import Product
from apps.tenants.services import TenantService
from apps.tenants.tests.auth import owner_access_token


def _tenant(slug: str):
    tenant, _ = TenantService.create_tenant(
        slug,
        slug,
        f'{slug}@test.com',
        'pass12345',
    )
    return tenant, owner_access_token(tenant)


def _account(tenant, marketplace: str, suffix: str, *, active=True):
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=marketplace,
        name=f'{marketplace.title()} {suffix}',
        external_id=f'{marketplace}-{tenant.pk}-{suffix}',
        credentials_enc=encrypt({'test': suffix}),
        is_active=active,
    )


def _product(tenant, suffix: str):
    return Product.objects.create(
        tenant=tenant,
        article=f'ART-{suffix}',
        name=f'Product {suffix}',
        title_ai=f'AI Product {suffix}',
        description_ai='Prepared description',
        brand='Brand',
        price=Decimal('100.00'),
        stock_qty=2,
    )


def _get(client: Client, token: str, product_id: int):
    return client.get(
        f'/api/v1/listings/workspace/{product_id}/',
        HTTP_AUTHORIZATION=f'Bearer {token}',
    )


@pytest.mark.django_db
def test_workspace_returns_local_channel_summaries_without_credentials():
    tenant, token = _tenant('workspace-summary')
    avito = _account(tenant, MarketplaceAccount.MARKETPLACE_AVITO, 'primary')
    ozon = _account(tenant, MarketplaceAccount.MARKETPLACE_OZON, 'primary')
    _account(
        tenant,
        MarketplaceAccount.MARKETPLACE_OZON,
        'inactive',
        active=False,
    )
    product = _product(tenant, 'summary')
    listing = Listing.objects.create(
        tenant=tenant,
        product=product,
        account=avito,
        title=product.title_ai,
        price_on_listing=Decimal('120.00'),
        status=Listing.STATUS_DRAFT,
    )
    draft = OzonOfferDraft.objects.create(
        tenant=tenant,
        product=product,
        account=ozon,
        publication_status='published',
        provider_product_id=123,
        provider_sku=456,
        provider_status='processed',
        moderation_status='approved',
    )

    response = _get(Client(), token, product.pk)

    assert response.status_code == 200
    data = response.json()['data']
    physical_profile = data['product']['physical_profile']
    assert physical_profile['missing_fields'] == [
        'barcode', 'length_mm', 'width_mm', 'height_mm', 'weight_g', 'vat_rate',
    ]
    assert {key: value for key, value in data['product'].items() if key != 'physical_profile'} == {
        'id': product.pk,
        'article': product.article,
        'name': product.name,
        'brand': 'Brand',
        'price': '100.00',
        'stock_qty': 2,
        'title_ai': product.title_ai,
        'description_ai': 'Prepared description',
    }
    assert {row['id'] for row in data['accounts']} == {avito.pk, ozon.pk}
    assert all('credentials' not in row for row in data['accounts'])
    assert data['avito_listings'] == [{
        'id': listing.pk,
        'account_id': avito.pk,
        'status': 'draft',
        'status_display': 'Черновик',
        'can_publish': True,
        'preflight_loaded': False,
    }]
    assert data['ozon_drafts'] == [{
        'id': draft.pk,
        'account_id': ozon.pk,
        'draft_exists': True,
        'publication_status': 'published',
        'provider_product_id': 123,
        'provider_sku': 456,
        'provider_status': 'processed',
        'moderation_status': 'approved',
        'provider_error_count': 0,
        'last_provider_sync_at': None,
        'external_url': 'https://www.ozon.ru/product/456/',
    }]


@pytest.mark.django_db
def test_workspace_enforces_product_and_account_tenant_fences():
    tenant, token = _tenant('workspace-fence')
    foreign, _ = _tenant('workspace-fence-foreign')
    product = _product(tenant, 'own')
    foreign_product = _product(foreign, 'foreign')
    _account(foreign, MarketplaceAccount.MARKETPLACE_OZON, 'foreign')

    response = _get(Client(), token, product.pk)
    foreign_response = _get(Client(), token, foreign_product.pk)

    assert response.status_code == 200
    assert response.json()['data']['accounts'] == []
    assert foreign_response.status_code == 404


@pytest.mark.django_db
def test_workspace_query_count_does_not_grow_with_ozon_account_count():
    tenant, token = _tenant('workspace-bounded')
    product = _product(tenant, 'bounded')
    first = _account(tenant, MarketplaceAccount.MARKETPLACE_OZON, 'one')
    OzonOfferDraft.objects.create(tenant=tenant, product=product, account=first)
    client = Client()

    with CaptureQueriesContext(connection) as baseline:
        assert _get(client, token, product.pk).status_code == 200

    for index in range(2, 9):
        account = _account(
            tenant,
            MarketplaceAccount.MARKETPLACE_OZON,
            str(index),
        )
        OzonOfferDraft.objects.create(
            tenant=tenant,
            product=product,
            account=account,
        )

    with CaptureQueriesContext(connection) as expanded:
        response = _get(client, token, product.pk)

    assert response.status_code == 200
    assert len(response.json()['data']['ozon_drafts']) == 8
    assert len(expanded) == len(baseline)
