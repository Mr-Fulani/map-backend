"""Импорт полного дерева категорий Avito в каталог тенанта.

Источник — apps/marketplaces/data/avito_tree_<domain>.json (его готовит команда
sync_avito_full_tree из API Avito): вложенное дерево категорий + самый глубокий
уровень — виды запчастей (Двигатель → Патрубки вентиляции и т.д.).

Строит из дерева TenantCatalogCategory с родителями. Идемпотентно.
"""
import json
from pathlib import Path

from django.db import OperationalError, ProgrammingError, transaction
from django.utils import timezone

from apps.products.avito_category_aliases import avito_aliases_by_normalized_name
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


def load_baked_tree(domain_slug: str) -> list[dict]:
    """Читает резервное вложенное дерево категорий домена из JSON."""
    data = json.loads(tree_path(domain_slug).read_text(encoding='utf-8'))
    return data.get('tree', [])


def load_tree(domain_slug: str) -> list[dict]:
    """Возвращает последний проверенный API-снимок либо резервное дерево из кода."""
    try:
        from apps.marketplaces.models import AvitoCategoryTreeSnapshot

        snapshot = AvitoCategoryTreeSnapshot.objects.filter(
            domain_slug=domain_slug,
            status=AvitoCategoryTreeSnapshot.STATUS_READY,
        ).first()
        if snapshot and snapshot.tree:
            return snapshot.tree
    except (OperationalError, ProgrammingError):
        # Команда migrate может импортировать сервисы до создания таблицы снимков.
        pass
    return load_baked_tree(domain_slug)


class AvitoTreeImporter:
    """Строит каталог тенанта из вшитого дерева Avito конкретного домена."""

    def __init__(self, domain_slug: str, tree: list[dict] | None = None):
        """Принимает готовое дерево (для тестов) либо читает avito_tree_<domain>.json."""
        self.domain_slug = domain_slug
        self.tree = tree if tree is not None else load_tree(domain_slug)
        self._aliases_by_normalized_name = avito_aliases_by_normalized_name()

    @transaction.atomic
    def import_for_tenant(self, tenant, *, reconcile: bool = False) -> int:
        """
        Создаёт или обновляет категории тенанта.

        При reconcile отсутствующие в новом снимке Avito-узлы мягко выключаются,
        но не удаляются: назначения товаров и наценки остаются сохранены.
        """
        domain = CatalogDomain.objects.filter(slug=self.domain_slug).first()
        if domain is None:
            return 0
        self._created = 0
        self._updated = 0
        self._seen_ids: set[int] = set()
        for node in self.tree:
            self._create(tenant, domain, node, parent=None)
        self._deactivated = 0
        if reconcile:
            stale = TenantCatalogCategory.objects.filter(
                tenant=tenant,
                root_domain=domain,
                external_source=EXTERNAL_SOURCE,
            ).exclude(pk__in=self._seen_ids).filter(is_active=True)
            self._deactivated = stale.update(is_active=False, updated_at=timezone.now())
        self.last_result = {
            'created': self._created,
            'updated': self._updated,
            'deactivated': self._deactivated,
        }
        return self._created

    def _create(self, tenant, domain, node: dict, parent):
        """Рекурсивно создаёт узел и его детей."""
        name = node.get('name')
        if not name:
            return
        normalized_name = normalize_category_name(name)
        aliases = self._aliases_by_normalized_name.get(normalized_name, [])
        category, created = TenantCatalogCategory.objects.get_or_create(
            tenant=tenant,
            parent=parent,
            normalized_name=normalized_name,
            defaults={
                'name': name,
                'root_domain': domain,
                'domain': domain.slug,
                'external_source': EXTERNAL_SOURCE,
                'external_id': node.get('slug') or '',
                'aliases': aliases,
                # Новые потомки выключенной пользователем ветки не должны
                # самовольно появляться в выборе категорий.
                'is_active': parent.is_active if parent is not None else True,
            },
        )
        self._created += int(created)
        self._seen_ids.add(category.pk)
        if not created:
            update_fields = []
            slug = node.get('slug') or ''
            if slug and category.external_id != slug:
                category.external_id = slug
                update_fields.append('external_id')
            if category.external_source != EXTERNAL_SOURCE:
                category.external_source = EXTERNAL_SOURCE
                update_fields.append('external_source')
            if category.root_domain_id != domain.pk:
                category.root_domain = domain
                update_fields.append('root_domain')
            if category.domain != domain.slug:
                category.domain = domain.slug
                update_fields.append('domain')
            if category.name != name:
                category.name = name
                update_fields.append('name')
            # Дозаполняем недостающие курируемые синонимы (нужны авто-классификации).
            missing = [alias for alias in aliases if alias not in category.aliases]
            if missing:
                category.aliases = [*category.aliases, *missing]
                update_fields.append('aliases')
            if update_fields:
                category.save(update_fields=[*update_fields, 'updated_at'])
                self._updated += 1
        for child in node.get('children', []):
            self._create(tenant, domain, child, parent=category)
