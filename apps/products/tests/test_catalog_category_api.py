from decimal import Decimal

import pytest
from django.test import Client

from apps.products.models import Product, TenantCatalogCategory
from apps.tenants.models import CatalogDomain, TenantCatalogDomain
from apps.tenants.tests.auth import create_tenant_with_operator_key


def _make_tenant(slug):
    return create_tenant_with_operator_key(
        slug, slug, f'{slug}@test.com', 'pass12345',
    )


def _make_category(tenant, name='Воздуховод'):
    domain = CatalogDomain.objects.filter(slug='auto_parts').first()
    return TenantCatalogCategory.objects.create(
        tenant=tenant, name=name, normalized_name=name.lower(),
        root_domain=domain, domain='auto_parts', is_active=True,
    )


def _enable_auto_parts(tenant):
    domain = CatalogDomain.objects.get(slug='auto_parts')
    TenantCatalogDomain.objects.update_or_create(
        tenant=tenant,
        domain=domain,
        defaults={'is_enabled': True},
    )
    return domain


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


@pytest.mark.django_db
class TestCatalogCategoryHierarchy:
    def _auth(self, api_key):
        return {'HTTP_AUTHORIZATION': f'Bearer {api_key}'}

    def test_list_returns_paths_and_hides_legacy_seed_when_avito_exists(self):
        tenant, api_key = _make_tenant('cat-hierarchy-list')
        domain = _enable_auto_parts(tenant)
        parent = TenantCatalogCategory.objects.create(
            tenant=tenant,
            name='Тестовая группа Avito',
            root_domain=domain,
            external_source='avito',
        )
        child = TenantCatalogCategory.objects.create(
            tenant=tenant,
            name='Тестовая конечная категория',
            parent=parent,
            root_domain=domain,
            external_source='avito',
        )
        legacy = TenantCatalogCategory.objects.create(
            tenant=tenant,
            name='Старый шаблон',
            root_domain=domain,
            external_source='platform_auto_parts_seed',
        )
        inactive = TenantCatalogCategory.objects.create(
            tenant=tenant,
            name='Отключённая категория',
            root_domain=domain,
            is_active=False,
        )

        response = Client().get(
            '/api/v1/products/catalog-categories/?assignable=true',
            **self._auth(api_key),
        )

        assert response.status_code == 200
        categories = {item['id']: item for item in response.json()['data']}
        assert legacy.pk not in categories
        assert inactive.pk not in categories
        assert categories[parent.pk]['path'] == ['Тестовая группа Avito']
        assert categories[parent.pk]['is_selectable'] is False
        assert categories[child.pk]['path'] == [
            'Тестовая группа Avito',
            'Тестовая конечная категория',
        ]
        assert categories[child.pk]['path_label'] == (
            'Тестовая группа Avito / Тестовая конечная категория'
        )
        assert categories[child.pk]['depth'] == 1
        assert categories[child.pk]['is_selectable'] is True

    def test_list_returns_inherited_margin_and_its_source(self):
        tenant, api_key = _make_tenant('cat-margin-inheritance')
        domain = _enable_auto_parts(tenant)
        parent = TenantCatalogCategory.objects.create(
            tenant=tenant,
            name='Двигатель',
            root_domain=domain,
            external_source='avito',
            default_margin_pct=Decimal('17.50'),
        )
        child = TenantCatalogCategory.objects.create(
            tenant=tenant,
            name='Головка блока цилиндров',
            parent=parent,
            root_domain=domain,
            external_source='avito',
            default_margin_pct=None,
        )

        response = Client().get(
            '/api/v1/products/catalog-categories/',
            **self._auth(api_key),
        )

        assert response.status_code == 200
        categories = {item['id']: item for item in response.json()['data']}
        assert categories[child.pk]['default_margin_pct'] is None
        assert Decimal(categories[child.pk]['effective_margin_pct']) == Decimal('17.50')
        assert categories[child.pk]['margin_inherited_from_id'] == parent.pk
        assert categories[child.pk]['margin_inherited_from_name'] == parent.name

        child.default_margin_pct = Decimal('0')
        child.save(update_fields=['default_margin_pct'])
        response = Client().get(
            '/api/v1/products/catalog-categories/',
            **self._auth(api_key),
        )
        categories = {item['id']: item for item in response.json()['data']}
        assert Decimal(categories[child.pk]['effective_margin_pct']) == Decimal('0')
        assert categories[child.pk]['margin_inherited_from_id'] is None

    def test_assign_rejects_parent_category(self):
        tenant, api_key = _make_tenant('cat-hierarchy-assign')
        domain = _enable_auto_parts(tenant)
        parent = TenantCatalogCategory.objects.create(
            tenant=tenant,
            name='Тестовый раздел',
            root_domain=domain,
            external_source='avito',
        )
        TenantCatalogCategory.objects.create(
            tenant=tenant,
            name='Тестовая подкатегория',
            parent=parent,
            root_domain=domain,
            external_source='avito',
        )
        product = Product.objects.create(
            tenant=tenant,
            article='TREE-1',
            name='Товар для дерева',
            price=Decimal('0'),
            stock_qty=0,
        )

        response = Client().post(
            '/api/v1/products/catalog-categories/assign/',
            {'product_ids': [product.pk], 'catalog_category': parent.pk},
            content_type='application/json',
            **self._auth(api_key),
        )

        assert response.status_code == 400
        assert response.json()['code'] == 'category_not_selectable'
        product.refresh_from_db()
        assert product.catalog_category_id is None

    def test_update_rejects_category_cycle(self):
        tenant, api_key = _make_tenant('cat-hierarchy-cycle')
        domain = _enable_auto_parts(tenant)
        parent = TenantCatalogCategory.objects.create(
            tenant=tenant,
            name='Родитель тестового цикла',
            root_domain=domain,
        )
        child = TenantCatalogCategory.objects.create(
            tenant=tenant,
            name='Потомок тестового цикла',
            parent=parent,
            root_domain=domain,
        )

        response = Client().patch(
            f'/api/v1/products/catalog-categories/{parent.pk}/',
            {'parent': child.pk},
            content_type='application/json',
            **self._auth(api_key),
        )

        assert response.status_code == 400
        assert response.json()['code'] == 'validation_error'
        parent.refresh_from_db()
        assert parent.parent_id is None
