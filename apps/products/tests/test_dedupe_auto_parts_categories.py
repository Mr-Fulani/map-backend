"""
Тесты management-команды dedupe_auto_parts_categories.

Покрывают три ветки: однозначный перенос товара на avito-категорию,
неоднозначное совпадение (несколько веток avito с тем же именем —
не переносим), отсутствие аналога (не переносим). Во всех трёх случаях
seed-категория должна быть удалена.
"""
from decimal import Decimal

import pytest
from django.core.management import call_command

from apps.products.models import Product, TenantCatalogCategory
from apps.tenants.models import CatalogDomain
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant


def make_category(tenant, domain, name, external_source, parent=None):
    return TenantCatalogCategory.objects.create(
        tenant=tenant, name=name, normalized_name=name.lower(),
        root_domain=domain, domain=domain.slug, parent=parent,
        external_source=external_source, is_active=True,
    )


def make_product(tenant, category, article):
    return Product.objects.create(
        tenant=tenant, article=article, name=f'Товар {article}',
        price=Decimal('100'), catalog_category=category,
    )


@pytest.mark.django_db
class TestDedupeAutoPartsCategories:
    def test_unambiguous_match_remaps_products_and_deletes_seed(self):
        tenant = make_tenant('dedupe-unambiguous-co')
        domain = CatalogDomain.objects.filter(slug='auto_parts').first()
        seed = make_category(tenant, domain, 'Тормозная система', 'platform_auto_parts_seed')
        avito_parent = make_category(tenant, domain, 'Двигатели и комплектующие', 'avito')
        avito = make_category(tenant, domain, 'Тормозная система', 'avito', parent=avito_parent)
        product = make_product(tenant, seed, 'ART-1')

        call_command('dedupe_auto_parts_categories')

        product.refresh_from_db()
        assert product.catalog_category_id == avito.id
        assert not TenantCatalogCategory.objects.filter(id=seed.id).exists()

    def test_ambiguous_match_leaves_product_uncategorized(self):
        tenant = make_tenant('dedupe-ambiguous-co')
        domain = CatalogDomain.objects.filter(slug='auto_parts').first()
        seed = make_category(tenant, domain, 'Двери', 'platform_auto_parts_seed')
        parent_a = make_category(tenant, domain, 'Легковые', 'avito')
        parent_b = make_category(tenant, domain, 'Грузовые', 'avito')
        make_category(tenant, domain, 'Двери', 'avito', parent=parent_a)
        make_category(tenant, domain, 'Двери', 'avito', parent=parent_b)
        product = make_product(tenant, seed, 'ART-2')

        call_command('dedupe_auto_parts_categories')

        product.refresh_from_db()
        assert product.catalog_category_id is None
        assert not TenantCatalogCategory.objects.filter(id=seed.id).exists()

    def test_no_match_leaves_product_uncategorized(self):
        tenant = make_tenant('dedupe-nomatch-co')
        domain = CatalogDomain.objects.filter(slug='auto_parts').first()
        seed = make_category(tenant, domain, 'Только в старом дереве', 'platform_auto_parts_seed')
        product = make_product(tenant, seed, 'ART-3')

        call_command('dedupe_auto_parts_categories')

        product.refresh_from_db()
        assert product.catalog_category_id is None
        assert not TenantCatalogCategory.objects.filter(id=seed.id).exists()

    def test_dry_run_changes_nothing(self):
        tenant = make_tenant('dedupe-dryrun-co')
        domain = CatalogDomain.objects.filter(slug='auto_parts').first()
        seed = make_category(tenant, domain, 'Тормозная система', 'platform_auto_parts_seed')
        avito_parent = make_category(tenant, domain, 'Двигатели и комплектующие', 'avito')
        avito = make_category(tenant, domain, 'Тормозная система', 'avito', parent=avito_parent)
        product = make_product(tenant, seed, 'ART-4')

        call_command('dedupe_auto_parts_categories', dry_run=True)

        product.refresh_from_db()
        assert product.catalog_category_id == seed.id
        assert TenantCatalogCategory.objects.filter(id=seed.id).exists()
        assert TenantCatalogCategory.objects.filter(id=avito.id).exists()

    def test_leaves_other_sources_untouched(self):
        """Категории без external_source (ручные, созданные тенантом) не трогаем."""
        tenant = make_tenant('dedupe-custom-co')
        domain = CatalogDomain.objects.filter(slug='auto_parts').first()
        custom = make_category(tenant, domain, 'Моя категория', '')

        call_command('dedupe_auto_parts_categories')

        assert TenantCatalogCategory.objects.filter(id=custom.id).exists()
