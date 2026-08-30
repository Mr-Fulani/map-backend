from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client

from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.products.models import Product, ProductPhysicalProfile
from apps.products.physical_profiles import physical_profile_presentation
from apps.products.services import ProductService
from apps.tenants.tests.auth import create_tenant_with_operator_key


def _tenant(slug: str):
    return create_tenant_with_operator_key(
        slug,
        slug,
        f'{slug}@test.com',
        'pass12345',
    )


def _datasource(tenant, source_type=DataSourceConnection.TYPE_1C_HTTP):
    return DataSourceConnection.objects.create(
        tenant=tenant,
        name='Test source',
        type=source_type,
        credentials=encrypt({
            'url': 'https://1c.example.com',
            'user': 'u',
            'password': 'p',
        }),
    )


def _source_item(**overrides):
    item = {
        'uuid': None,
        'article': 'PHYS-100',
        'name': 'Амортизатор',
        'brand': 'Test',
        'price': '1000.00',
        'stock_qty': 3,
        'category': 'Подвеска',
        'condition': 'new',
    }
    item.update(overrides)
    return item


def _api(client: Client, api_key: str, method: str, url: str, payload=None):
    return getattr(client, method)(
        url,
        payload or {},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )


@pytest.mark.django_db
def test_valid_1c_values_win_without_overwriting_map_fallback():
    tenant, _api_key = _tenant('physical-source')
    datasource = _datasource(tenant)
    product, _status, _change = ProductService.upsert_from_source(
        tenant,
        datasource,
        _source_item(
            barcode='4601234567890',
            length_cm='25.5',
            width_mm='120',
            height_mm='80',
            weight_kg='1.25',
            vat='0.2',
        ),
    )
    profile = product.physical_profile
    profile.map_barcode = 'MAP-BARCODE'
    profile.map_length_mm = Decimal('999')
    profile.map_vat_rate = Decimal('10')
    profile.save()

    product, _status, _change = ProductService.upsert_from_source(
        tenant,
        datasource,
        _source_item(
            barcode='4601234567890',
            length_cm='26',
            width_mm='120',
            height_mm='80',
            weight_kg='1.25',
            vat='20%',
        ),
    )
    profile.refresh_from_db()
    assert profile.source_length_mm == Decimal('260.000')
    assert profile.source_weight_g == Decimal('1250.000')
    assert profile.source_vat_rate == Decimal('20.00')
    assert profile.map_barcode == 'MAP-BARCODE'
    assert profile.map_length_mm == Decimal('999.000')
    assert profile.map_vat_rate == Decimal('10.00')

    data = physical_profile_presentation(product)
    assert data['facts']['barcode']['effective_source'] == '1c'
    assert data['facts']['barcode']['effective_value'] == '4601234567890'
    assert data['facts']['length_mm']['effective_value'] == '260'
    assert data['facts']['vat_rate']['effective_value'] == '20'
    assert data['complete'] is True


@pytest.mark.django_db
def test_missing_or_invalid_1c_values_fall_back_to_map_and_record_safe_errors():
    tenant, _api_key = _tenant('physical-fallback')
    datasource = _datasource(tenant)
    product, _status, _change = ProductService.upsert_from_source(
        tenant,
        datasource,
        _source_item(length_mm='100', weight_g='500', vat_rate='20'),
    )
    profile = product.physical_profile
    profile.map_length_mm = Decimal('250')
    profile.map_weight_g = Decimal('700')
    profile.map_vat_rate = Decimal('10')
    profile.save()

    ProductService.upsert_from_source(
        tenant,
        datasource,
        _source_item(length_mm='-1', weight_g='not-a-number', vat_rate='18'),
    )
    profile.refresh_from_db()
    assert profile.source_length_mm is None
    assert profile.source_weight_g is None
    assert profile.source_vat_rate is None
    assert set(profile.source_errors) == {'length_mm', 'weight_g', 'vat_rate'}
    assert 'not-a-number' not in str(profile.source_errors)

    product.refresh_from_db()
    data = physical_profile_presentation(product)
    assert data['facts']['length_mm']['effective_source'] == 'map'
    assert data['facts']['length_mm']['effective_value'] == '250'
    assert data['facts']['weight_g']['effective_value'] == '700'
    assert data['facts']['vat_rate']['effective_value'] == '10'


@pytest.mark.django_db
def test_csv_values_do_not_masquerade_as_1c_provenance():
    tenant, _api_key = _tenant('physical-csv')
    datasource = _datasource(tenant, DataSourceConnection.TYPE_CSV)
    product, _status, _change = ProductService.upsert_from_source(
        tenant,
        datasource,
        _source_item(length_mm='100', weight_g='500', vat_rate='20'),
    )

    assert not ProductPhysicalProfile.objects.filter(product=product).exists()


@pytest.mark.django_db
def test_empty_1c_physical_payload_does_not_create_sparse_profile():
    tenant, _api_key = _tenant('physical-empty-source')
    datasource = _datasource(tenant)
    product, _status, _change = ProductService.upsert_from_source(
        tenant,
        datasource,
        _source_item(
            barcode='', length_mm='', width_mm='', height_mm='', weight_g='', vat_rate='',
        ),
    )

    assert not ProductPhysicalProfile.objects.filter(product=product).exists()


@pytest.mark.django_db
def test_batch_import_updates_source_profiles_without_touching_map_values():
    tenant, _api_key = _tenant('physical-batch')
    datasource = _datasource(tenant)
    products = ProductService.upsert_batch_from_source(
        tenant,
        datasource,
        [
            _source_item(article='BATCH-1', weight_g='500'),
            _source_item(article='BATCH-2', weight_g='750'),
        ],
    )
    first = products[0][0]
    first.physical_profile.map_weight_g = Decimal('900')
    first.physical_profile.save()

    ProductService.upsert_batch_from_source(
        tenant,
        datasource,
        [
            _source_item(article='BATCH-1', weight_g='600'),
            _source_item(article='BATCH-2', weight_g='800'),
        ],
    )

    first.physical_profile.refresh_from_db()
    assert first.physical_profile.source_weight_g == Decimal('600.000')
    assert first.physical_profile.map_weight_g == Decimal('900.000')
    assert (
        ProductPhysicalProfile.objects.get(product=products[1][0]).source_weight_g
        == Decimal('800.000')
    )


@pytest.mark.django_db
def test_get_empty_profile_is_read_only_and_reports_missing_fields():
    tenant, api_key = _tenant('physical-empty')
    product = Product.objects.create(
        tenant=tenant,
        article='EMPTY-1',
        name='Товар',
        price=Decimal('0'),
        stock_qty=0,
    )

    response = _api(
        Client(), api_key, 'get', f'/api/v1/products/{product.pk}/physical-profile/',
    )

    assert response.status_code == 200
    assert response.json()['data']['complete'] is False
    assert response.json()['data']['missing_fields'] == [
        'barcode', 'length_mm', 'width_mm', 'height_mm', 'weight_g', 'vat_rate',
    ]
    assert not ProductPhysicalProfile.objects.filter(product=product).exists()


@pytest.mark.django_db
def test_patch_updates_only_map_fallback_and_embeds_profile_in_product_detail():
    tenant, api_key = _tenant('physical-patch')
    datasource = _datasource(tenant)
    product, _status, _change = ProductService.upsert_from_source(
        tenant,
        datasource,
        _source_item(barcode='SOURCE-CODE', length_mm='300'),
    )
    original_source_updated_at = product.physical_profile.source_updated_at

    with patch('apps.products.views.sync_product_listings_task.delay') as avito_sync:
        response = _api(
            Client(),
            api_key,
            'patch',
            f'/api/v1/products/{product.pk}/physical-profile/',
            {
                'barcode': 'MAP-CODE',
                'length_mm': '100',
                'width_mm': '200',
                'height_mm': '50',
                'weight_g': '1500',
                'vat_rate': '7',
            },
        )
    avito_sync.assert_not_called()

    assert response.status_code == 200
    data = response.json()['data']
    assert data['facts']['barcode']['source_value'] == 'SOURCE-CODE'
    assert data['facts']['barcode']['map_value'] == 'MAP-CODE'
    assert data['facts']['barcode']['effective_source'] == '1c'
    assert data['facts']['width_mm']['effective_source'] == 'map'
    profile = ProductPhysicalProfile.objects.get(product=product)
    assert profile.source_updated_at == original_source_updated_at
    assert profile.source_length_mm == Decimal('300.000')
    assert profile.map_length_mm == Decimal('100.000')

    detail = _api(Client(), api_key, 'get', f'/api/v1/products/{product.pk}/')
    assert detail.status_code == 200
    assert detail.json()['data']['physical_profile']['facts']['vat_rate']['map_value'] == '7'


@pytest.mark.django_db
@pytest.mark.parametrize('payload', [
    {'weight_g': '-1'},
    {'vat_rate': '18'},
    {'source_barcode': 'forbidden'},
    {},
])
def test_patch_rejects_invalid_or_source_owned_fields(payload):
    tenant, api_key = _tenant(f'physical-invalid-{len(str(payload))}')
    product = Product.objects.create(
        tenant=tenant,
        article='INVALID-1',
        name='Товар',
        price=Decimal('0'),
        stock_qty=0,
    )

    response = _api(
        Client(), api_key, 'patch',
        f'/api/v1/products/{product.pk}/physical-profile/', payload,
    )

    assert response.status_code == 400
    assert not ProductPhysicalProfile.objects.filter(product=product).exists()


@pytest.mark.django_db
def test_profile_endpoint_is_tenant_scoped():
    tenant_a, api_key_a = _tenant('physical-tenant-a')
    tenant_b, api_key_b = _tenant('physical-tenant-b')
    product = Product.objects.create(
        tenant=tenant_a,
        article='TENANT-A',
        name='Товар A',
        price=Decimal('0'),
        stock_qty=0,
    )

    response = _api(
        Client(), api_key_b, 'patch',
        f'/api/v1/products/{product.pk}/physical-profile/',
        {'weight_g': '100'},
    )
    assert response.status_code == 404
    assert not ProductPhysicalProfile.objects.filter(product=product).exists()

    own_response = _api(
        Client(), api_key_a, 'patch',
        f'/api/v1/products/{product.pk}/physical-profile/',
        {'weight_g': '100'},
    )
    assert own_response.status_code == 200
