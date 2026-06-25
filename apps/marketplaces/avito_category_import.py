"""Импорт официального дерева категорий Avito в каталог тенанта.

Источник — справочник apps/marketplaces/data/avito_field_specs.json (его готовит
команда sync_avito_categories из API Avito): 192 листа под «Запчасти и аксессуары»
с путём (path), slug и фиксированными полями (Category/GoodsType/ProductType).

Импорт строит из путей иерархию TenantCatalogCategory и создаёт CategoryMapping
(категория-лист → Avito-категория + атрибуты), чтобы публикация подставляла
правильные GoodsType/ProductType, а не дефолтную категорию.
"""
import json
from pathlib import Path

from django.db import transaction

from apps.marketplaces.models import CategoryMapping
from apps.products.models import TenantCatalogCategory
from apps.products.part_category_seed import normalize_category_name
from apps.tenants.models import CatalogDomain

SPECS_PATH = Path(__file__).resolve().parent / 'data' / 'avito_field_specs.json'
AUTO_PARTS_SLUG = 'auto_parts'
DEFAULT_AVITO_CATEGORY = 'Запчасти и аксессуары'


def load_avito_leaves(path=SPECS_PATH) -> list[dict]:
    """Читает листья дерева Avito из JSON-справочника."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return data.get('leaves', [])


class AvitoCatalogImporter:
    """Импортирует дерево категорий Avito в каталог тенанта и создаёт маппинги."""

    EXTERNAL_SOURCE = 'avito'

    def __init__(self, leaves: list[dict] | None = None):
        """Принимает готовый список листьев (для тестов) либо читает справочник."""
        self.leaves = leaves if leaves is not None else load_avito_leaves()

    @transaction.atomic
    def import_for_tenant(self, tenant) -> dict:
        """
        Создаёт у тенанта категории и маппинги по дереву Avito. Идемпотентно.

        Возвращает {'categories': N, 'mappings': M} — сколько создано нового.
        """
        domain = CatalogDomain.objects.filter(slug=AUTO_PARTS_SLUG).first()
        if domain is None:
            return {'categories': 0, 'mappings': 0}

        categories_created = 0
        mappings_created = 0
        for leaf in self.leaves:
            # path[0] == «Запчасти и аксессуары» — это сам авто-домен, его пропускаем.
            chain = (leaf.get('path') or [])[1:]
            if not chain:
                continue

            parent = None
            for depth, name in enumerate(chain):
                is_leaf = depth == len(chain) - 1
                external_id = leaf['slug'] if is_leaf else f'avito:{normalize_category_name(name)}'
                category, created = TenantCatalogCategory.objects.get_or_create(
                    tenant=tenant,
                    parent=parent,
                    normalized_name=normalize_category_name(name),
                    defaults={
                        'name': name,
                        'root_domain': domain,
                        'domain': domain.slug,
                        'external_source': self.EXTERNAL_SOURCE,
                        'external_id': external_id,
                        'is_active': True,
                    },
                )
                categories_created += int(created)
                parent = category

            fixed = leaf.get('fixed') or {}
            _, mapping_created = CategoryMapping.objects.get_or_create(
                tenant=tenant,
                marketplace=CategoryMapping.MARKETPLACE_AVITO,
                category_source=leaf['name'],
                defaults={
                    'category_target': fixed.get('Category', DEFAULT_AVITO_CATEGORY),
                    'category_id': 0,
                    'attributes_map': fixed,
                },
            )
            mappings_created += int(mapping_created)

        return {'categories': categories_created, 'mappings': mappings_created}
