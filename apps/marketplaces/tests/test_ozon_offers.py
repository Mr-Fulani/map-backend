from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from apps.datasources.encryption import encrypt
from apps.marketplaces.models import (
    Listing,
    MarketplaceAccount,
    MarketplaceFeedRun,
    OzonAccountProfile,
    OzonCategoryTreeSnapshot,
    OzonOfferDraft,
)
from apps.marketplaces.ozon_catalog import normalize_category_tree
from apps.products.models import Product, ProductImage, ProductPhysicalProfile
from apps.tenants.tests.auth import create_tenant_with_operator_key


def _tenant(slug):
    return create_tenant_with_operator_key(
        slug, slug, f'{slug}@test.com', 'pass12345',
    )


def _account(tenant, external_id, *, marketplace='ozon'):
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=marketplace,
        name=f'{marketplace} {external_id}',
        external_id=external_id,
        credentials_enc=encrypt({
            'client_id': external_id,
            'api_key' if marketplace == 'ozon' else 'client_secret': 'secret',
        }),
    )
    if marketplace == 'ozon':
        OzonAccountProfile.objects.create(
            account=account,
            connection_status=OzonAccountProfile.ConnectionStatus.CONNECTED,
            selected_warehouse_id='warehouse-1',
            selected_warehouse_name='Склад 1',
            last_checked_at=timezone.now(),
        )
    return account


def _product(tenant, article='OZ-1'):
    return Product.objects.create(
        tenant=tenant,
        article=article,
        name='Амортизатор',
        brand='Test Brand',
        price=Decimal('1000'),
        stock_qty=2,
        description_ai='Описание товара',
    )


def _catalog(account):
    tree, node_count, active_type_count = normalize_category_tree([{
        'description_category_id': 101,
        'category_name': 'Автотовары',
        'disabled': False,
        'children': [{
            'type_id': 202,
            'type_name': 'Амортизатор',
            'disabled': False,
            'children': [],
        }],
    }])
    return OzonCategoryTreeSnapshot.objects.create(
        account=account,
        language='DEFAULT',
        schema_hash='a' * 64,
        tree=tree,
        node_count=node_count,
        active_type_count=active_type_count,
    )


def _complete_product(product):
    ProductPhysicalProfile.objects.create(
        tenant=product.tenant,
        product=product,
        source_barcode='4600000000000',
        source_length_mm=Decimal('100'),
        source_width_mm=Decimal('80'),
        source_height_mm=Decimal('50'),
        source_weight_g=Decimal('500'),
        source_vat_rate=Decimal('20'),
    )
    ProductImage.objects.create(
        product=product,
        s3_key='products/test.jpg',
        sha256='a' * 64,
    )


def _request(client, key, method, product, payload=None, account_id=None):
    url = f'/api/v1/products/{product.pk}/ozon-offer/'
    if account_id is not None:
        url += f'?account_id={account_id}'
    return getattr(client, method)(
        url,
        payload or {},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {key}',
    )


@pytest.mark.django_db
def test_offer_identity_is_stable_account_scoped_and_never_enters_avito_runtime():
    tenant, key = _tenant('ozon-offer')
    account = _account(tenant, 'client-1')
    product = _product(tenant)
    _catalog(account)
    client = Client()

    empty = _request(client, key, 'get', product, account_id=account.pk)
    started = _request(client, key, 'patch', product, {'account_id': account.pk})
    selected = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'description_category_id': 101,
        'type_id': 202,
    })

    assert empty.status_code == started.status_code == selected.status_code == 200
    assert empty.json()['data']['draft'] is None
    offer_id = started.json()['data']['draft']['offer_id']
    assert selected.json()['data']['draft']['offer_id'] == offer_id
    account.name = 'Переименованный кабинет'
    account.save(update_fields=['name', 'updated_at'])
    reread = _request(client, key, 'get', product, account_id=account.pk)
    assert reread.json()['data']['draft']['offer_id'] == offer_id
    assert OzonOfferDraft.objects.get().category_path == 'Автотовары'
    assert Listing.objects.count() == MarketplaceFeedRun.objects.count() == 0


@pytest.mark.django_db
def test_offer_identity_is_separate_for_two_ozon_accounts_of_one_tenant():
    tenant, key = _tenant('ozon-multi-account')
    first = _account(tenant, 'client-first')
    second = _account(tenant, 'client-second')
    product = _product(tenant)
    _catalog(first)
    _catalog(second)
    client = Client()

    first_response = _request(client, key, 'patch', product, {'account_id': first.pk})
    second_response = _request(client, key, 'patch', product, {'account_id': second.pk})

    assert first_response.status_code == second_response.status_code == 200
    first_draft = first_response.json()['data']['draft']
    second_draft = second_response.json()['data']['draft']
    assert first_draft['offer_id'] != second_draft['offer_id']
    assert OzonOfferDraft.objects.filter(tenant=tenant, product=product).count() == 2
    assert Listing.objects.count() == MarketplaceFeedRun.objects.count() == 0


@pytest.mark.django_db
def test_offer_api_fences_tenant_and_provider_and_rejects_non_leaf_category():
    tenant, key = _tenant('ozon-fence')
    other, other_key = _tenant('ozon-fence-other')
    account = _account(tenant, 'client-fence')
    avito = _account(tenant, 'avito-fence', marketplace='avito')
    product = _product(tenant)
    _catalog(account)

    foreign_product = _request(Client(), other_key, 'patch', product, {'account_id': account.pk})
    wrong_account = _request(Client(), key, 'patch', product, {'account_id': avito.pk})
    invalid_leaf = _request(Client(), key, 'patch', product, {
        'account_id': account.pk,
        'description_category_id': 101,
        'type_id': 999,
    })

    assert foreign_product.status_code == wrong_account.status_code == 404
    assert invalid_leaf.status_code == 400
    assert invalid_leaf.json()['code'] == 'invalid_category_type'
    assert not OzonOfferDraft.objects.exists()
    assert other.products.count() == 0


@pytest.mark.django_db
def test_basic_preflight_is_ready_after_category_and_required_product_facts():
    tenant, key = _tenant('ozon-preflight')
    account = _account(tenant, 'client-preflight')
    product = _product(tenant)
    _catalog(account)
    _complete_product(product)
    client = Client()

    ready = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'description_category_id': 101,
        'type_id': 202,
    })

    assert ready.status_code == 200
    assert ready.json()['data']['preflight'] == {
        'ready': True,
        'errors': [],
        'recommendations': [],
    }
