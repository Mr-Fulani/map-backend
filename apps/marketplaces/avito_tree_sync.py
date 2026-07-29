"""Безопасная периодическая синхронизация дерева категорий Avito."""

import hashlib
import json
import logging
import time
from dataclasses import dataclass

import requests
from django.db import transaction
from django.utils import timezone

from apps.marketplaces.adapters.avito.adapter import AvitoAdapter
from apps.marketplaces.avito_tree_import import (
    AvitoTreeImporter,
    load_baked_tree,
)
from apps.marketplaces.models import (
    AvitoCategoryTreeSnapshot,
    MarketplaceAccount,
)
from apps.tenants.models import TenantCatalogDomain

logger = logging.getLogger(__name__)

AUTO_PARTS_DOMAIN = 'auto_parts'
AUTO_PARTS_ROOT = 'Запчасти и аксессуары'
AUTO_PARTS_PATH = ('Транспорт', AUTO_PARTS_ROOT)
MIN_AUTO_PARTS_NODES = 100
MIN_PREVIOUS_TREE_RATIO = 0.65


class AvitoTreeSyncError(Exception):
    """Дерево не удалось получить или безопасно применить."""


@dataclass
class TreeBuildResult:
    tree: list[dict]
    warnings: list[str]


def _children(node: dict) -> list[dict]:
    nested = node.get('nested') or []
    if isinstance(nested, dict):
        nested = [
            item
            for values in nested.values()
            for item in (values if isinstance(values, list) else [values])
        ]
    return [item for item in nested if isinstance(item, dict)]


def _find_path(nodes: list[dict], path: tuple[str, ...]):
    current = nodes
    node = None
    for name in path:
        node = next((item for item in current if item.get('name') == name), None)
        if node is None:
            return None
        current = _children(node)
    return node


def _node_count(nodes: list[dict]) -> int:
    return sum(1 + _node_count(node.get('children') or []) for node in nodes)


def _paths(nodes: list[dict], parent=()) -> set[tuple[str, ...]]:
    result = set()
    for node in nodes:
        path = parent + (str(node.get('name') or ''),)
        result.add(path)
        result.update(_paths(node.get('children') or [], path))
    return result


def _previous_children_by_slug(nodes: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for node in nodes:
        slug = node.get('slug')
        if slug:
            result[str(slug)] = node.get('children') or []
        result.update(_previous_children_by_slug(node.get('children') or []))
    return result


class AvitoLiveTreeBuilder:
    """Строит полное дерево, включая значения полей *SparePartType."""

    def __init__(self, adapter: AvitoAdapter, previous_tree: list[dict]):
        self.adapter = adapter
        self.previous_children = _previous_children_by_slug(previous_tree)
        self.warnings: list[str] = []

    def build_auto_parts(self) -> TreeBuildResult:
        raw_tree = self.adapter.get_category_tree()
        root = _find_path(raw_tree, AUTO_PARTS_PATH)
        if root is None:
            raise AvitoTreeSyncError(
                'Avito API не вернул ветку «Транспорт → Запчасти и аксессуары».'
            )
        tree = [self._build_node(node) for node in _children(root)]
        return TreeBuildResult(tree=tree, warnings=self.warnings)

    def _build_node(self, node: dict) -> dict:
        children = _children(node)
        result = {
            'name': node.get('name'),
            'slug': node.get('slug'),
            'children': [],
        }
        if children:
            result['children'] = [self._build_node(child) for child in children]
            return result

        slug = str(node.get('slug') or '')
        if not slug:
            return result
        try:
            values = self._leaf_part_types(slug, str(node.get('name') or ''))
        except Exception as exc:  # noqa: BLE001 — сохраняем предыдущую проверенную ветку
            fallback = self.previous_children.get(slug, [])
            result['children'] = fallback
            self.warnings.append(
                f'{slug}: не удалось обновить подвиды, сохранена предыдущая версия ({exc})'
            )
            return result

        result['children'] = [
            {'name': value, 'slug': None, 'children': []}
            for value in values
        ]
        return result

    def _leaf_part_types(self, slug: str, leaf_name: str) -> list[str]:
        for attempt in range(6):
            try:
                data = self.adapter.get_node_fields(slug)
                break
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if status == 429 and attempt < 5:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise
            except requests.RequestException:
                if attempt < 5:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise

        values: list[str] = []
        for field in data.get('fields', []):
            tag = str(field.get('tag') or '')
            if not tag.endswith('SparePartType'):
                continue
            for content in field.get('content') or []:
                inline = [
                    str(value.get('value'))
                    for value in content.get('values') or []
                    if value.get('value')
                ]
                if not inline and content.get('values_link_json'):
                    inline = self._fetch_link_values(content['values_link_json'])
                values.extend(value for value in inline if value != leaf_name)
        time.sleep(0.1)
        return list(dict.fromkeys(values))

    def _fetch_link_values(self, url: str) -> list[str]:
        token = self.adapter._auth.get_token(self.adapter.account)
        for attempt in range(6):
            response = requests.get(
                url,
                headers={'Authorization': f'Bearer {token}'},
                timeout=30,
            )
            if response.status_code == 429 and attempt < 5:
                time.sleep(3 * (attempt + 1))
                continue
            response.raise_for_status()
            return [
                str(value.get('value'))
                for value in response.json().get('values') or []
                if value.get('value')
            ]
        raise AvitoTreeSyncError('Avito не вернул значения поля после повторных попыток.')


class AvitoCategoryTreeSyncService:
    """Получает, проверяет, сохраняет и применяет актуальное дерево."""

    @classmethod
    def _source_accounts(cls) -> list[MarketplaceAccount]:
        accounts = list(
            MarketplaceAccount.objects.filter(
                marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
                is_active=True,
                tenant__is_active=True,
            )
            .select_related('tenant')
            .order_by('pk')
        )
        if not accounts:
            raise AvitoTreeSyncError('Нет активного аккаунта Avito для обновления дерева.')
        return accounts

    @classmethod
    def sync_auto_parts(cls) -> dict:
        accounts = cls._source_accounts()
        snapshot = AvitoCategoryTreeSnapshot.objects.filter(
            domain_slug=AUTO_PARTS_DOMAIN,
        ).first()
        previous_tree = (
            snapshot.tree
            if snapshot and snapshot.status == AvitoCategoryTreeSnapshot.STATUS_READY and snapshot.tree
            else load_baked_tree(AUTO_PARTS_DOMAIN)
        )

        attempts = []
        for account in accounts:
            try:
                result = AvitoLiveTreeBuilder(
                    AvitoAdapter(account),
                    previous_tree,
                ).build_auto_parts()
                summary = cls._validate(previous_tree, result.tree)
                return cls._store_and_apply(account, snapshot, result, summary)
            except Exception as exc:  # noqa: BLE001 — пробуем следующий активный аккаунт
                attempts.append(f'account={account.pk}: {exc}')
                logger.warning(
                    'Аккаунт %s не подошёл для синхронизации дерева Avito: %s',
                    account.pk,
                    exc,
                )

        error = AvitoTreeSyncError(
            'Ни один активный аккаунт Avito не смог получить дерево. '
            + '; '.join(attempts[:5])
        )
        cls._store_error(accounts[-1], snapshot, error)
        raise error

    @classmethod
    def _validate(cls, previous_tree: list[dict], tree: list[dict]) -> dict:
        node_count = _node_count(tree)
        previous_count = _node_count(previous_tree)
        if node_count < MIN_AUTO_PARTS_NODES:
            raise AvitoTreeSyncError(
                f'Получено подозрительно короткое дерево: {node_count} узлов.'
            )
        if previous_count and node_count < previous_count * MIN_PREVIOUS_TREE_RATIO:
            raise AvitoTreeSyncError(
                f'Новое дерево меньше предыдущего: {node_count} вместо {previous_count}.'
            )
        current_paths = _paths(tree)
        previous_paths = _paths(previous_tree)
        return {
            'node_count': node_count,
            'previous_node_count': previous_count,
            'change_count': len(current_paths ^ previous_paths),
            'added_count': len(current_paths - previous_paths),
            'removed_count': len(previous_paths - current_paths),
        }

    @classmethod
    @transaction.atomic
    def _store_and_apply(cls, account, snapshot, result, summary) -> dict:
        now = timezone.now()
        canonical = json.dumps(
            result.tree,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        checksum = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        snapshot, _ = AvitoCategoryTreeSnapshot.objects.select_for_update().update_or_create(
            domain_slug=AUTO_PARTS_DOMAIN,
            defaults={
                'root_name': AUTO_PARTS_ROOT,
                'tree': result.tree,
                'checksum': checksum,
                'status': AvitoCategoryTreeSnapshot.STATUS_READY,
                'node_count': summary['node_count'],
                'change_count': summary['change_count'],
                'fetched_at': now,
                'last_error': '',
                'source_account': account,
                'metadata': {
                    **summary,
                    'warnings': result.warnings[:50],
                    'warning_count': len(result.warnings),
                    'last_attempt_status': 'ready',
                    'last_attempt_at': now.isoformat(),
                },
            },
        )

        totals = {'tenants': 0, 'created': 0, 'updated': 0, 'deactivated': 0}
        tenant_domains = (
            TenantCatalogDomain.objects.filter(
                domain__slug=AUTO_PARTS_DOMAIN,
                is_enabled=True,
                tenant__is_active=True,
            )
            .select_related('tenant')
            .order_by('tenant_id')
        )
        for tenant_domain in tenant_domains:
            importer = AvitoTreeImporter(AUTO_PARTS_DOMAIN, tree=result.tree)
            importer.import_for_tenant(tenant_domain.tenant, reconcile=True)
            totals['tenants'] += 1
            for key in ('created', 'updated', 'deactivated'):
                totals[key] += importer.last_result[key]

        snapshot.applied_at = now
        snapshot.metadata = {**snapshot.metadata, **totals}
        snapshot.save(update_fields=['applied_at', 'metadata', 'updated_at'])
        return {
            'domain': AUTO_PARTS_DOMAIN,
            'checksum': checksum,
            **summary,
            **totals,
            'warning_count': len(result.warnings),
        }

    @classmethod
    def _store_error(cls, account, snapshot, exc) -> None:
        now = timezone.now()
        if snapshot is not None:
            snapshot.fetched_at = now
            snapshot.last_error = str(exc)[:500]
            snapshot.source_account = account
            snapshot.metadata = {
                **(snapshot.metadata or {}),
                'last_attempt_status': 'error',
                'last_attempt_at': now.isoformat(),
            }
            snapshot.save(update_fields=[
                'fetched_at', 'last_error', 'source_account', 'metadata', 'updated_at',
            ])
        else:
            AvitoCategoryTreeSnapshot.objects.create(
                domain_slug=AUTO_PARTS_DOMAIN,
                root_name=AUTO_PARTS_ROOT,
                status=AvitoCategoryTreeSnapshot.STATUS_ERROR,
                fetched_at=now,
                last_error=str(exc)[:500],
                source_account=account,
                tree=[],
                checksum='',
                node_count=0,
                metadata={
                    'last_attempt_status': 'error',
                    'last_attempt_at': now.isoformat(),
                },
            )
        logger.exception('Не удалось обновить дерево категорий Avito: %s', exc)
