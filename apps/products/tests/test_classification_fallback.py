from decimal import Decimal

import pytest

from apps.products.models import Product, TenantCatalogCategory
from apps.products.services import ProductEnrichmentService, dehomoglyph
from apps.tenants.models import CatalogDomain, TenantCatalogDomain
from apps.tenants.services import TenantService


def test_dehomoglyph_latin_lookalikes_to_cyrillic():
    # «Oпopa шapoвaя» — латинские O/o/p/a вместо кириллицы.
    assert dehomoglyph('Oпopa шapoвaя').lower() == 'опора шаровая'


def _setup(slug):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@t.com', 'pass12345')
    domain, _ = CatalogDomain.objects.get_or_create(
        slug='auto_parts', defaults={'name': 'Автозапчасти'},
    )
    TenantCatalogDomain.objects.get_or_create(tenant=tenant, domain=domain, defaults={'is_enabled': True})
    return tenant, domain


def _cat(tenant, domain, name, source='platform_auto_parts_seed', aliases=None):
    return TenantCatalogCategory.objects.create(
        tenant=tenant, name=name, normalized_name=name.lower().replace(' ', ''),
        root_domain=domain, domain='auto_parts', external_source=source,
        aliases=aliases or [], is_active=True,
    )


def _product(tenant, name):
    return Product.objects.create(
        tenant=tenant, article=name[:20], name=name, brand='X',
        price=Decimal('0'), stock_qty=0,
    )


@pytest.mark.django_db
class TestClassificationFallback:
    def test_homoglyph_product_matches_category(self):
        tenant, domain = _setup('cls-homo')
        _cat(tenant, domain, 'Шаровые опоры', aliases=['Опора шаровая', 'Шаровая'])
        product = _product(tenant, 'Oпopa шapoвaя')  # латинские двойники

        result = ProductEnrichmentService.infer_product_tenant_category(product)
        assert result is not None
        assert result.name == 'Шаровые опоры'

    def test_fallback_to_generic_avito_node_when_no_match(self):
        tenant, domain = _setup('cls-fallback')
        _cat(tenant, domain, 'Шаровые опоры', aliases=['Опора шаровая'])
        fallback = _cat(tenant, domain, 'Автомобиль на запчасти', source='avito')
        product = _product(tenant, 'Загадочная деталь QWERTY ZZZ')

        result = ProductEnrichmentService.infer_product_tenant_category(product)
        assert result is not None
        assert result.id == fallback.id

    def test_no_fallback_without_avito_generic_node(self):
        tenant, domain = _setup('cls-nofallback')
        _cat(tenant, domain, 'Шаровые опоры', aliases=['Опора шаровая'])
        product = _product(tenant, 'Загадочная деталь QWERTY ZZZ')

        # Нет узла «Автомобиль на запчасти» (avito) → остаётся None (не выдумываем).
        assert ProductEnrichmentService.infer_product_tenant_category(product) is None

    def test_alias_enables_match_on_two_word_category_without_full_name_overlap(self):
        """Регрессия: категория «Фонари и фары» без единого совпадения по имени
        товара всё равно находится, если у неё есть alias «Фонарь» — то есть
        именно то, что восстанавливает backfill_avito_category_aliases."""
        tenant, domain = _setup('cls-alias-match')
        target = _cat(tenant, domain, 'Фонари и фары', source='avito', aliases=['Фонарь'])
        fallback = _cat(tenant, domain, 'Автомобиль на запчасти', source='avito')

        product = _product(tenant, 'Фонарь задний левый на крышку багажника VW Polo')

        result = ProductEnrichmentService.infer_product_tenant_category(product)
        assert result is not None
        assert result.id == target.id
        assert result.id != fallback.id
