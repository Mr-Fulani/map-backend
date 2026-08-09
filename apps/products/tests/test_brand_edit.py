"""Тесты ручного редактирования бренда товара и его защиты от импорта."""
from decimal import Decimal

import pytest
from django.test import Client
from unittest.mock import patch

from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.products.models import Product, ProductBrand, ProductBrandAlias
from apps.products.services import ProductService
from apps.tenants.tests.auth import create_tenant_with_operator_key


def _tenant(slug):
    return create_tenant_with_operator_key(
        slug, slug, f'{slug}@test.com', 'pass12345',
    )


def _datasource(tenant):
    return DataSourceConnection.objects.create(
        tenant=tenant,
        name='Test Source',
        type=DataSourceConnection.TYPE_1C_HTTP,
        credentials=encrypt({'url': 'http://example.com', 'user': 'u', 'password': 'p'}),
    )


def _source_item(brand='', price='100.00'):
    return {
        'uuid': None,
        'article': 'BR-100',
        'name': 'Фонарь правый Hyundai Solaris',
        'brand': brand,
        'price': price,
        'stock_qty': 3,
        'category': 'Оптика',
        'condition': 'new',
    }


@pytest.mark.django_db
class TestProductBrandPatch:
    def test_api_key_patch_keeps_unknown_brand_tenant_local(
        self,
        django_capture_on_commit_callbacks,
    ):
        tenant, api_key = _tenant('brand-edit-co')
        product = Product.objects.create(
            tenant=tenant, article='A1', name='Фонарь', brand='',
            price=Decimal('0'), stock_qty=0,
        )
        client = Client()

        with patch('apps.products.views.sync_product_listings_task.delay') as sync_delay:
            with django_capture_on_commit_callbacks(execute=True):
                response = client.patch(
                    f'/api/v1/products/{product.pk}/',
                    {'brand': '  Tenant-Only Brand 9  '},
                    content_type='application/json',
                    HTTP_AUTHORIZATION=f'Bearer {api_key}',
                )

        assert response.status_code == 200
        product.refresh_from_db()
        assert product.brand == 'Tenant-Only Brand 9'
        assert product.brand_ref is None
        assert product.brand_resolution_status == Product.BrandResolutionStatus.SOURCE
        assert product.brand_confidence == 0.5
        assert product.brand_source_id == 'api_key'
        assert product.brand_needs_review is True
        assert not ProductBrand.objects.filter(
            normalized_name='TENANTONLYBRAND9',
        ).exists()
        assert not ProductBrandAlias.objects.filter(
            normalized_alias='TENANTONLYBRAND9',
        ).exists()
        sync_delay.assert_called_once_with(product.pk, 'content')

    def test_api_key_patch_can_link_existing_catalog_brand(
        self,
        django_capture_on_commit_callbacks,
    ):
        tenant, api_key = _tenant('brand-existing-co')
        brand_ref = ProductBrand.objects.create(
            name='Existing Catalog Brand',
            source_id='platform_seed',
            confidence=0.95,
        )
        product = Product.objects.create(
            tenant=tenant, article='A1B', name='Фонарь', brand='',
            price=Decimal('0'), stock_qty=0,
        )

        with patch('apps.products.views.sync_product_listings_task.delay'):
            with django_capture_on_commit_callbacks(execute=True):
                response = Client().patch(
                    f'/api/v1/products/{product.pk}/',
                    {'brand': 'Existing Catalog Brand'},
                    content_type='application/json',
                    HTTP_AUTHORIZATION=f'Bearer {api_key}',
                )

        assert response.status_code == 200
        product.refresh_from_db()
        assert product.brand_ref == brand_ref
        assert product.brand_resolution_status == Product.BrandResolutionStatus.CATALOG
        assert product.brand_source_id == 'catalog'
        assert product.brand_confidence == 0.95
        assert product.brand_needs_review is False
        assert ProductBrand.objects.filter(pk=brand_ref.pk).count() == 1
        assert not ProductBrandAlias.objects.filter(
            normalized_alias='EXISTINGCATALOGBRAND',
        ).exists()

    def test_patch_requires_brand_field(self):
        tenant, api_key = _tenant('brand-edit-400-co')
        product = Product.objects.create(
            tenant=tenant, article='A2', name='Фонарь', brand='X',
            price=Decimal('0'), stock_qty=0,
        )
        client = Client()

        response = client.patch(
            f'/api/v1/products/{product.pk}/',
            {'name': 'Другое имя'},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {api_key}',
        )

        assert response.status_code == 400

    def test_patch_foreign_tenant_product_returns_404(self):
        tenant, api_key = _tenant('brand-edit-own-co')
        other_tenant, _ = _tenant('brand-edit-other-co')
        product = Product.objects.create(
            tenant=other_tenant, article='A3', name='Фонарь', brand='',
            price=Decimal('0'), stock_qty=0,
        )
        client = Client()

        response = client.patch(
            f'/api/v1/products/{product.pk}/',
            {'brand': 'TYC'},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {api_key}',
        )

        assert response.status_code == 404

    def test_brand_options_include_current_brand(self):
        tenant, api_key = _tenant('brand-options-co')
        product = Product.objects.create(
            tenant=tenant, article='A4', name='Фонарь', brand='Редкий бренд',
            price=Decimal('0'), stock_qty=0,
        )
        client = Client()

        response = client.get(
            f'/api/v1/products/brand-options/?product_id={product.pk}',
            HTTP_AUTHORIZATION=f'Bearer {api_key}',
        )

        assert response.status_code == 200
        assert {'name': 'Редкий бренд', 'source': 'current'} in response.json()['data']['options']


@pytest.mark.django_db
class TestBrandSurvivesImport:
    def test_manual_brand_not_wiped_by_import_with_empty_brand(self):
        """Тенант дозаполнил бренд вручную → импорт из 1С с пустым брендом
        (и изменившейся ценой) не затирает его."""
        tenant, _ = _tenant('brand-keep-co')
        ds = _datasource(tenant)
        product, _, _ = ProductService.upsert_from_source(tenant, ds, _source_item())
        product.brand = 'Hyundai-KIA'
        product.brand_resolution_status = Product.BrandResolutionStatus.MANUAL
        product.brand_confidence = 1.0
        product.brand_source_id = 'manual'
        product.save(update_fields=[
            'brand', 'brand_resolution_status', 'brand_confidence', 'brand_source_id',
        ])

        ProductService.upsert_from_source(tenant, ds, _source_item(price='150.00'))

        product.refresh_from_db()
        assert product.brand == 'Hyundai-KIA'
        assert product.brand_resolution_status == Product.BrandResolutionStatus.MANUAL
        assert product.brand_source_id == 'manual'

    def test_source_brand_still_updates_product(self):
        """Если источник присылает непустой бренд — он применяется как раньше."""
        tenant, _ = _tenant('brand-src-co')
        ds = _datasource(tenant)
        product, _, _ = ProductService.upsert_from_source(tenant, ds, _source_item())
        product.brand = 'Ручной'
        product.save(update_fields=['brand'])

        ProductService.upsert_from_source(tenant, ds, _source_item(brand='TYC', price='150.00'))

        product.refresh_from_db()
        assert product.brand == 'TYC'
        assert product.brand_resolution_status == Product.BrandResolutionStatus.SOURCE
        assert product.brand_source_id == ds.type
