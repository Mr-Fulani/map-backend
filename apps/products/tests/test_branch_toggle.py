"""Тесты каскадного включения/отключения ветки категорий каталога."""
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client

from apps.products.models import Product, ProductCatalogClassification, TenantCatalogCategory, TenantCategoryMapping
from apps.products.services import ProductEnrichmentService
from apps.products.tasks import reclassify_products_for_categories
from apps.tenants.models import CatalogDomain, TenantCatalogDomain
from apps.tenants.services import TenantService


def _tenant(slug):
    tenant, api_key = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    domain, _ = CatalogDomain.objects.get_or_create(slug='auto_parts', defaults={'name': 'Автозапчасти'})
    TenantCatalogDomain.objects.get_or_create(tenant=tenant, domain=domain, defaults={'is_enabled': True})
    return tenant, api_key, domain


def _cat(tenant, domain, name, parent=None, source='avito'):
    return TenantCatalogCategory.objects.create(
        tenant=tenant, name=name, normalized_name='',
        root_domain=domain, domain='auto_parts', external_source=source,
        is_active=True, parent=parent,
    )


def _product(tenant, name, category=None):
    return Product.objects.create(
        tenant=tenant, article=name[:20], name=name, brand='X',
        price=Decimal('0'), stock_qty=0, catalog_category=category,
    )


def _truck_tree(tenant, domain):
    trucks = _cat(tenant, domain, 'Для грузовиков и спецтехники')
    brakes = _cat(tenant, domain, 'Тормозная система', parent=trucks)
    cabin = _cat(tenant, domain, 'Кабина', parent=trucks)
    bumpers = _cat(tenant, domain, 'Бампера', parent=cabin)
    return trucks, brakes, cabin, bumpers


@pytest.mark.django_db
class TestBranchToggle:
    def test_disable_cascades_to_subtree_and_queues_reclassification(
        self, django_capture_on_commit_callbacks,
    ):
        tenant, api_key, domain = _tenant('branch-off')
        trucks, brakes, cabin, bumpers = _truck_tree(tenant, domain)
        product = _product(tenant, 'Абсорбер бампера', category=bumpers)
        client = Client()

        with patch('apps.products.tasks.reclassify_products_for_categories.delay') as delay:
            with django_capture_on_commit_callbacks(execute=True):
                response = client.post(
                    f'/api/v1/products/catalog-categories/{trucks.pk}/toggle-branch/',
                    {'is_active': False},
                    content_type='application/json',
                    HTTP_AUTHORIZATION=f'Bearer {api_key}',
                )

        assert response.status_code == 200
        data = response.json()['data']
        assert data['affected_categories'] == 4
        assert data['affected_products'] == 1
        for category in (trucks, brakes, cabin, bumpers):
            category.refresh_from_db()
            assert category.is_active is False
        assert delay.call_count == 1
        called_tenant_id, called_ids = delay.call_args[0]
        assert called_tenant_id == tenant.pk
        assert sorted(called_ids) == sorted([trucks.pk, brakes.pk, cabin.pk, bumpers.pk])
        product.refresh_from_db()
        assert product.catalog_category_id == bumpers.pk  # переносит уже сама задача

    def test_enable_cascades_to_subtree(self):
        tenant, api_key, domain = _tenant('branch-on')
        trucks, brakes, cabin, bumpers = _truck_tree(tenant, domain)
        TenantCatalogCategory.objects.filter(tenant=tenant).update(is_active=False)
        client = Client()

        response = client.post(
            f'/api/v1/products/catalog-categories/{trucks.pk}/toggle-branch/',
            {'is_active': True},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {api_key}',
        )

        assert response.status_code == 200
        assert response.json()['data']['affected_categories'] == 4
        for category in (trucks, brakes, cabin, bumpers):
            category.refresh_from_db()
            assert category.is_active is True

    def test_invalid_payload_returns_400(self):
        tenant, api_key, domain = _tenant('branch-bad')
        trucks, *_ = _truck_tree(tenant, domain)
        client = Client()

        response = client.post(
            f'/api/v1/products/catalog-categories/{trucks.pk}/toggle-branch/',
            {'is_active': 'да'},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {api_key}',
        )

        assert response.status_code == 400

    def test_reclassify_task_moves_products_out_of_disabled_branch(self):
        tenant, _, domain = _tenant('branch-task')
        trucks, brakes, cabin, bumpers = _truck_tree(tenant, domain)
        target = _cat(tenant, domain, 'Кузов')
        target_child = TenantCatalogCategory.objects.create(
            tenant=tenant, name='Бамперы', normalized_name='', root_domain=domain,
            domain='auto_parts', external_source='avito', is_active=True, parent=target,
            aliases=['Бампер'],
        )
        product = _product(tenant, 'Абсорбер бампера HYUNDAI SOLARIS', category=bumpers)
        branch_ids = [trucks.pk, brakes.pk, cabin.pk, bumpers.pk]
        TenantCatalogCategory.objects.filter(id__in=branch_ids).update(is_active=False)

        result = reclassify_products_for_categories(tenant.pk, branch_ids)

        assert result['reclassified'] == 1
        product.refresh_from_db()
        assert product.catalog_category_id == target_child.pk

    def test_reclassify_task_skips_manual_classification(self):
        tenant, _, domain = _tenant('branch-manual')
        trucks, brakes, cabin, bumpers = _truck_tree(tenant, domain)
        product = _product(tenant, 'Абсорбер бампера', category=bumpers)
        ProductCatalogClassification.objects.create(
            tenant=tenant, product=product,
            domain=ProductCatalogClassification.Domain.AUTO_PARTS,
            confidence=1.0, source=ProductCatalogClassification.Source.MANUAL,
        )

        result = reclassify_products_for_categories(tenant.pk, [bumpers.pk])

        assert result['reclassified'] == 0
        product.refresh_from_db()
        assert product.catalog_category_id == bumpers.pk

    def test_mapping_to_disabled_category_is_ignored(self):
        tenant, _, domain = _tenant('branch-mapping')
        disabled = _cat(tenant, domain, 'Для грузовиков и спецтехники')
        disabled.is_active = False
        disabled.save(update_fields=['is_active'])
        active = _cat(tenant, domain, 'Подвеска')
        active.aliases = ['Амортизатор']
        active.save(update_fields=['aliases'])
        TenantCategoryMapping.objects.create(tenant=tenant, source_category='Грузовое', category=disabled)
        product = _product(tenant, 'Амортизатор передний')
        product.category_1c = 'Грузовое'
        product.save(update_fields=['category_1c'])

        result = ProductEnrichmentService.get_product_tenant_category(product)

        assert result is not None
        assert result.pk == active.pk
