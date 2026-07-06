"""Импорт полного дерева категорий Avito в каталог тенанта.

Источник — apps/marketplaces/data/avito_tree_<domain>.json (его готовит команда
sync_avito_full_tree из API Avito): вложенное дерево категорий + самый глубокий
уровень — виды запчастей (Двигатель → Патрубки вентиляции и т.д.).

Строит из дерева TenantCatalogCategory с родителями. Идемпотентно.
"""
import json
from pathlib import Path

from django.db import transaction

from apps.products.models import TenantCatalogCategory
from apps.products.part_category_seed import normalize_category_name
from apps.tenants.models import CatalogDomain

DATA_DIR = Path(__file__).resolve().parent / 'data'
EXTERNAL_SOURCE = 'avito'


def tree_path(domain_slug: str) -> Path:
    """Путь к JSON-дереву домена."""
    return DATA_DIR / f'avito_tree_{domain_slug}.json'


def has_tree(domain_slug: str) -> bool:
    """Есть ли вшитое дерево Avito для домена."""
    return tree_path(domain_slug).exists()


def load_tree(domain_slug: str) -> list[dict]:
    """Читает вложенное дерево категорий домена из JSON."""
    data = json.loads(tree_path(domain_slug).read_text(encoding='utf-8'))
    return data.get('tree', [])


class AvitoTreeImporter:
    """Строит каталог тенанта из вшитого дерева Avito конкретного домена."""

    def __init__(self, domain_slug: str, tree: list[dict] | None = None):
        """Принимает готовое дерево (для тестов) либо читает avito_tree_<domain>.json."""
        self.domain_slug = domain_slug
        self.tree = tree if tree is not None else load_tree(domain_slug)

    @transaction.atomic
    def import_for_tenant(self, tenant) -> int:
        """Создаёт у тенанта категории по дереву. Идемпотентно. Возвращает число созданных."""
        domain = CatalogDomain.objects.filter(slug=self.domain_slug).first()
        if domain is None:
            return 0
        self._created = 0
        for node in self.tree:
            self._create(tenant, domain, node, parent=None)
        return self._created

    def _create(self, tenant, domain, node: dict, parent):
        """Рекурсивно создаёт узел и его детей."""
        name = node.get('name')
        if not name:
            return
        category, created = TenantCatalogCategory.objects.get_or_create(
            tenant=tenant,
            parent=parent,
            normalized_name=normalize_category_name(name),
            defaults={
                'name': name,
                'root_domain': domain,
                'domain': domain.slug,
                'external_source': EXTERNAL_SOURCE,
                'external_id': node.get('slug') or '',
                'is_active': True,
            },
        )
        self._created += int(created)
        # Лечим ранее созданные записи без slug: по external_id (а не имени)
        # feed_builder резолвит лист Avito, имена листьев не уникальны.
        slug = node.get('slug') or ''
        if not created and slug and not category.external_id:
            category.external_id = slug
            category.save(update_fields=['external_id', 'updated_at'])
        for child in node.get('children', []):
            self._create(tenant, domain, child, parent=category)
