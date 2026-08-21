from unittest.mock import patch

import pytest

from apps.products.models import TenantCatalogCategory
from apps.tenants.models import CatalogDomain, TenantCatalogDomain
from apps.tenants.tests.auth import create_tenant_with_operator_key, owner_client


def _domain(slug, name, sort_order):
    domain, _ = CatalogDomain.objects.update_or_create(
        slug=slug,
        defaults={
            'name': name,
            'short_name': name,
            'is_active': True,
            'sort_order': sort_order,
        },
    )
    return domain


def _category(tenant, domain, name):
    return TenantCatalogCategory.objects.create(
        tenant=tenant,
        root_domain=domain,
        domain=domain.slug,
        name=name,
        normalized_name='',
        is_active=True,
    )


@pytest.mark.django_db
def test_catalog_domain_selection_replaces_all_enabled_domains_atomically():
    tenant, _ = create_tenant_with_operator_key(
        'Catalog selection',
        'catalog-selection',
        'catalog-selection@example.com',
        'pass12345',
    )
    auto_parts = _domain('auto_parts', 'Автозапчасти', 10)
    apparel = _domain('apparel', 'Одежда', 20)
    jewellery = _domain('jewellery', 'Украшения', 30)
    for domain in (auto_parts, apparel, jewellery):
        TenantCatalogDomain.objects.update_or_create(
            tenant=tenant,
            domain=domain,
            defaults={'is_enabled': True},
        )
        _category(tenant, domain, f'Категория {domain.slug}')

    with patch(
        'apps.products.services.ProductCategorySeedService.seed_tenant_primary_categories',
    ) as seed:
        response = owner_client(tenant).put(
            '/api/v1/catalog-domains/',
            {'enabled_domain_slugs': ['auto_parts']},
            content_type='application/json',
        )

    assert response.status_code == 200
    enabled = set(
        TenantCatalogDomain.objects.filter(
            tenant=tenant,
            is_enabled=True,
        ).values_list('domain__slug', flat=True)
    )
    assert enabled == {'auto_parts'}
    assert {
        item['slug']
        for item in response.json()['data']
        if item['is_enabled_for_tenant']
    } == {'auto_parts'}
    seed.assert_not_called()

    categories = owner_client(tenant).get('/api/v1/products/catalog-categories/')
    assert categories.status_code == 200
    assert {
        item['root_domain_slug']
        for item in categories.json()['data']
    } == {'auto_parts'}


@pytest.mark.django_db
def test_catalog_domain_selection_seeds_only_newly_enabled_domains():
    tenant, _ = create_tenant_with_operator_key(
        'Catalog enable',
        'catalog-enable',
        'catalog-enable@example.com',
        'pass12345',
    )
    auto_parts = _domain('auto_parts', 'Автозапчасти', 10)
    apparel = _domain('apparel', 'Одежда', 20)
    TenantCatalogDomain.objects.update_or_create(
        tenant=tenant,
        domain=auto_parts,
        defaults={'is_enabled': True},
    )
    TenantCatalogDomain.objects.update_or_create(
        tenant=tenant,
        domain=apparel,
        defaults={'is_enabled': False},
    )

    with patch(
        'apps.products.services.ProductCategorySeedService.seed_tenant_primary_categories',
    ) as seed:
        response = owner_client(tenant).put(
            '/api/v1/catalog-domains/',
            {'enabled_domain_slugs': ['auto_parts', 'apparel', 'apparel']},
            content_type='application/json',
        )

    assert response.status_code == 200
    seed.assert_called_once_with(tenant, apparel)


@pytest.mark.django_db
def test_catalog_domain_selection_rejects_unknown_domain_without_partial_update():
    tenant, _ = create_tenant_with_operator_key(
        'Catalog invalid',
        'catalog-invalid',
        'catalog-invalid@example.com',
        'pass12345',
    )
    auto_parts = _domain('auto_parts', 'Автозапчасти', 10)
    TenantCatalogDomain.objects.update_or_create(
        tenant=tenant,
        domain=auto_parts,
        defaults={'is_enabled': True},
    )

    with patch(
        'apps.products.services.ProductCategorySeedService.seed_tenant_primary_categories',
    ) as seed:
        response = owner_client(tenant).put(
            '/api/v1/catalog-domains/',
            {'enabled_domain_slugs': ['auto_parts', 'does-not-exist']},
            content_type='application/json',
        )

    assert response.status_code == 400
    assert TenantCatalogDomain.objects.get(
        tenant=tenant,
        domain=auto_parts,
    ).is_enabled is True
    seed.assert_not_called()
