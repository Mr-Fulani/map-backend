import types

import pytest

from apps.marketplaces.models import CategoryMapping
from apps.products.models import TenantCatalogCategory
from apps.tenants.models import CatalogDomain
from apps.tenants.services import TenantService


def _make_tenant(slug):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant


def _auto_parts_domain():
    domain, _ = CatalogDomain.objects.get_or_create(slug='auto_parts', defaults={'name': 'Автозапчасти'})
    return domain


def _category(tenant, name):
    return TenantCatalogCategory.objects.create(
        tenant=tenant, name=name, normalized_name=name.lower(),
        root_domain=_auto_parts_domain(), domain='auto_parts', is_active=True,
    )


def _mapping(tenant, source, attrs):
    return CategoryMapping.objects.create(
        tenant=tenant, marketplace=CategoryMapping.MARKETPLACE_AVITO,
        category_source=source, category_target='Запчасти и аксессуары',
        category_id=0, attributes_map=attrs,
    )


@pytest.mark.django_db
class TestCategoryMappingResolution:
    """Фид резолвит CategoryMapping по category_1c (приоритет) и по имени catalog_category."""

    def test_resolves_by_catalog_category_name_when_no_category_1c(self):
        from apps.marketplaces.adapters.avito.feed_builder import _get_category_mapping
        tenant = _make_tenant('map-resolve-co')
        category = _category(tenant, 'Двигатель')
        _mapping(tenant, 'Двигатель', {'GoodsType': 'Запчасти'})

        product = types.SimpleNamespace(category_1c='', catalog_category=category)
        listing = types.SimpleNamespace(tenant=tenant, product=product)

        mapping = _get_category_mapping(listing)
        assert mapping is not None
        assert mapping.category_source == 'Двигатель'
        assert mapping.attributes_map.get('GoodsType') == 'Запчасти'

    def test_category_1c_takes_priority_over_catalog_category(self):
        from apps.marketplaces.adapters.avito.feed_builder import _get_category_mapping
        tenant = _make_tenant('map-priority-co')
        category = _category(tenant, 'Двигатель')
        _mapping(tenant, 'Двигатель', {'GoodsType': 'Запчасти'})
        _mapping(tenant, 'Моторные масла', {'GoodsType': 'Масла и автохимия'})

        product = types.SimpleNamespace(category_1c='Моторные масла', catalog_category=category)
        listing = types.SimpleNamespace(tenant=tenant, product=product)

        assert _get_category_mapping(listing).category_source == 'Моторные масла'

    def test_has_resolved_category_true_with_catalog_category(self):
        from apps.marketplaces.adapters.avito.feed_builder import has_resolved_category
        tenant = _make_tenant('cat-resolved-co')
        category = _category(tenant, 'Двигатель')

        product = types.SimpleNamespace(category_1c='', catalog_category=category)
        listing = types.SimpleNamespace(tenant=tenant, product=product)
        assert has_resolved_category(listing) is True

    def test_has_resolved_category_false_when_undetermined(self):
        from apps.marketplaces.adapters.avito.feed_builder import has_resolved_category
        tenant = _make_tenant('cat-undetermined-co')

        product = types.SimpleNamespace(category_1c='', catalog_category=None)
        listing = types.SimpleNamespace(tenant=tenant, product=product)
        assert has_resolved_category(listing) is False
