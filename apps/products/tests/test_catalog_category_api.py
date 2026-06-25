from decimal import Decimal

import pytest
from django.test import Client

from apps.products.models import Product, TenantCatalogCategory
from apps.tenants.models import CatalogDomain
from apps.tenants.services import TenantService


def _make_tenant(slug):
    tenant, api_key = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant, api_key


def _make_category(tenant, name='Воздуховод'):
    domain = CatalogDomain.objects.filter(slug='auto_parts').first()
    return TenantCatalogCategory.objects.create(
        tenant=tenant, name=name, normalized_name=name.lower(),
        root_domain=domain, domain='auto_parts', is_active=True,
    )


@pytest.mark.django_db
class TestCatalogCategoryDelete:
    def _auth(self, api_key):
        return {'HTTP_AUTHORIZATION': f'Bearer {api_key}'}

    def test_soft_delete_disables_by_default(self):
        tenant, api_key = _make_tenant('cat-soft-co')
        category = _make_category(tenant)

        resp = Client().delete(f'/api/v1/products/catalog-categories/{category.id}/', **self._auth(api_key))

        assert resp.status_code == 204
        category.refresh_from_db()
        assert category.is_active is False  # запись осталась, лишь отключена

    def test_hard_delete_removes_category(self):
        tenant, api_key = _make_tenant('cat-hard-co')
        category = _make_category(tenant)

        resp = Client().delete(
            f'/api/v1/products/catalog-categories/{category.id}/?hard=true', **self._auth(api_key),
        )

        assert resp.status_code == 204
        assert not TenantCatalogCategory.objects.filter(id=category.id).exists()

    def test_hard_delete_blocked_when_products_attached(self):
        tenant, api_key = _make_tenant('cat-hard-block-co')
        category = _make_category(tenant)
        Product.objects.create(
            tenant=tenant, article='A1', brand='B', name='товар',
            price=Decimal('0'), stock_qty=0, catalog_category=category,
        )

        resp = Client().delete(
            f'/api/v1/products/catalog-categories/{category.id}/?hard=true', **self._auth(api_key),
        )

        assert resp.status_code == 409
        assert TenantCatalogCategory.objects.filter(id=category.id).exists()

    def test_hard_delete_blocked_when_has_children(self):
        tenant, api_key = _make_tenant('cat-hard-child-co')
        parent = _make_category(tenant, name='Двигатель')
        TenantCatalogCategory.objects.create(
            tenant=tenant, name='Воздуховод', normalized_name='воздуховод',
            root_domain=parent.root_domain, domain='auto_parts', parent=parent, is_active=True,
        )

        resp = Client().delete(
            f'/api/v1/products/catalog-categories/{parent.id}/?hard=true', **self._auth(api_key),
        )

        assert resp.status_code == 409
        assert TenantCatalogCategory.objects.filter(id=parent.id).exists()
