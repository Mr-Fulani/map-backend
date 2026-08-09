"""Безопасная периодическая синхронизация дерева категорий Avito."""

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.url_security import REDIRECT_NONE, request_public_http_url
from apps.marketplaces.adapters.avito.adapter import AVITO_API_BASE, AvitoAdapter
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
_ABSOLUTE_MAX_DEPTH = 32
_ABSOLUTE_MAX_NODES = 20_000
_ABSOLUTE_MAX_LEAVES = 10_000
_ABSOLUTE_MAX_TOTAL_CALLS = 10_000


class AvitoTreeSyncError(Exception):
    """Дерево не удалось получить или безопасно применить."""


class AvitoTreeLimitExceeded(AvitoTreeSyncError):
    """Ответ или обход Avito превысил жёсткий бюджет синхронизации."""


def _configured_limit(name: str, default: int, absolute_maximum: int) -> int:
    value = int(getattr(settings, name, default))
    return min(absolute_maximum, max(1, value))


@dataclass
class AvitoTreeCallBudget:
    """Shared request budget across all account fallbacks in one sync run."""

    maximum: int
    used: int = 0

    @classmethod
    def from_settings(cls):
        return cls(_configured_limit(
            'AVITO_TREE_MAX_TOTAL_CALLS', 3000, _ABSOLUTE_MAX_TOTAL_CALLS,
        ))

    def consume(self) -> None:
        if self.used >= self.maximum:
            raise AvitoTreeLimitExceeded(
                f'Превышен лимит API-вызовов дерева Avito: {self.maximum}.',
            )
        self.used += 1


def _validated_avito_api_url(value: str) -> str:
    """Accept dynamic values links only on the authenticated Avito API origin."""
    try:
        candidate = urlsplit(str(value or '').strip())
        expected = urlsplit(AVITO_API_BASE)
        candidate_port = candidate.port or 443
    except ValueError as exc:
        raise AvitoTreeSyncError('Avito вернул некорректную ссылку значений.') from exc
    if (
        candidate.scheme != 'https'
        or candidate.hostname != expected.hostname
        or candidate_port != 443
        or candidate.username is not None
        or candidate.password is not None
        or candidate.fragment
    ):
        raise AvitoTreeSyncError('Avito вернул ссылку значений с недоверенным origin.')
    return candidate.geturl()


def _request_avito_values(url: str, token: str):
    return request_public_http_url(
        _validated_avito_api_url(url),
        timeout=(5, 30),
        headers={
            'Accept': 'application/json',
            'Authorization': f'Bearer {token}',
        },
        max_response_bytes=settings.AVITO_API_RESPONSE_MAX_BYTES,
        redirect_policy=REDIRECT_NONE,
    )


@dataclass
class TreeBuildResult:
    tree: list[dict]
    warnings: list[str]


def _children(node: dict) -> list[dict]:
    nested = node.get('nested')
    if nested is None:
        return []
    if isinstance(nested, dict):
        flattened = []
        for values in nested.values():
            if isinstance(values, dict):
                flattened.append(values)
            elif isinstance(values, list):
                flattened.extend(values)
            else:
                raise AvitoTreeSyncError('Avito вернул некорректное поле nested.')
        nested = flattened
    if not isinstance(nested, list):
        raise AvitoTreeSyncError('Avito вернул некорректное поле nested.')
    if not all(isinstance(item, dict) for item in nested):
        raise AvitoTreeSyncError('Avito вернул некорректный дочерний узел.')
    return nested


def _find_path(nodes: list[dict], path: tuple[str, ...]):
    if not isinstance(nodes, list):
        raise AvitoTreeSyncError('Avito вернул дерево не в виде списка.')
    current = nodes
    node = None
    for name in path:
        if not all(isinstance(item, dict) for item in current):
            raise AvitoTreeSyncError('Avito вернул некорректный узел дерева.')
        node = next((item for item in current if item.get('name') == name), None)
        if node is None:
            return None
        current = _children(node)
    return node


def _walk_stored_tree(nodes: list[dict]):
    if not isinstance(nodes, list):
        raise AvitoTreeLimitExceeded('Дерево Avito должно быть списком.')
    max_depth = _configured_limit('AVITO_TREE_MAX_DEPTH', 12, _ABSOLUTE_MAX_DEPTH)
    max_nodes = _configured_limit('AVITO_TREE_MAX_NODES', 10_000, _ABSOLUTE_MAX_NODES)
    stack = [(node, (), 1) for node in reversed(nodes)]
    seen = 0
    while stack:
        node, parent, depth = stack.pop()
        if not isinstance(node, dict):
            raise AvitoTreeLimitExceeded('Дерево Avito содержит некорректный узел.')
        if depth > max_depth:
            raise AvitoTreeLimitExceeded(
                f'Превышена глубина дерева Avito: {max_depth}.',
            )
        seen += 1
        if seen > max_nodes:
            raise AvitoTreeLimitExceeded(
                f'Превышено число узлов дерева Avito: {max_nodes}.',
            )
        name = node.get('name')
        if name is not None and not isinstance(name, str):
            raise AvitoTreeSyncError('Имя узла дерева Avito должно быть строкой.')
        path = parent + (name or '',)
        yield node, path, depth
        children = node.get('children')
        if children is None:
            children = []
        if not isinstance(children, list):
            raise AvitoTreeSyncError('Поле children дерева Avito должно быть списком.')
        stack.extend((child, path, depth + 1) for child in reversed(children))


def _node_count(nodes: list[dict]) -> int:
    return sum(1 for _node, _path, _depth in _walk_stored_tree(nodes))


def _paths(nodes: list[dict], parent=()) -> set[tuple[str, ...]]:
    return {
        parent + path
        for _node, path, _depth in _walk_stored_tree(nodes)
    }


def _previous_children_by_slug(nodes: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for node, _path, _depth in _walk_stored_tree(nodes):
        slug = node.get('slug')
        if slug:
            children = node.get('children') or []
            result[str(slug)] = children
    return result


class AvitoLiveTreeBuilder:
    """Строит полное дерево, включая значения полей *SparePartType."""

    def __init__(
        self,
        adapter: AvitoAdapter,
        previous_tree: list[dict],
        *,
        call_budget: AvitoTreeCallBudget | None = None,
    ):
        self.adapter = adapter
        self.max_depth = _configured_limit(
            'AVITO_TREE_MAX_DEPTH', 12, _ABSOLUTE_MAX_DEPTH,
        )
        self.max_nodes = _configured_limit(
            'AVITO_TREE_MAX_NODES', 10_000, _ABSOLUTE_MAX_NODES,
        )
        self.max_leaves = _configured_limit(
            'AVITO_TREE_MAX_LEAVES', 2000, _ABSOLUTE_MAX_LEAVES,
        )
        self.call_budget = call_budget or AvitoTreeCallBudget.from_settings()
        self.nodes_seen = 0
        self.leaves_seen = 0
        self.previous_children = _previous_children_by_slug(previous_tree)
        self.warnings: list[str] = []

    def build_auto_parts(self) -> TreeBuildResult:
        self.call_budget.consume()
        raw_tree = self.adapter.get_category_tree()
        root = _find_path(raw_tree, AUTO_PARTS_PATH)
        if root is None:
            raise AvitoTreeSyncError(
                'Avito API не вернул ветку «Транспорт → Запчасти и аксессуары».'
            )
        tree = []
        for node in _children(root):
            tree.append(self._build_node(node, depth=1))
        return TreeBuildResult(tree=tree, warnings=self.warnings)

    def _build_node(self, node: dict, *, depth: int) -> dict:
        self._reserve_nodes(1, depth)
        children = _children(node)
        result = {
            'name': node.get('name'),
            'slug': node.get('slug'),
            'children': [],
        }
        if children:
            result['children'] = [
                self._build_node(child, depth=depth + 1)
                for child in children
            ]
            return result

        self.leaves_seen += 1
        if self.leaves_seen > self.max_leaves:
            raise AvitoTreeLimitExceeded(
                f'Превышено число листьев дерева Avito: {self.max_leaves}.',
            )
        slug = str(node.get('slug') or '')
        if not slug:
            return result
        try:
            values = self._leaf_part_types(slug, str(node.get('name') or ''))
        except AvitoTreeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 — сохраняем предыдущую проверенную ветку
            fallback = self.previous_children.get(slug, [])
            self._reserve_existing_subtree(fallback, depth=depth + 1)
            result['children'] = fallback
            self.warnings.append(
                f'{slug}: не удалось обновить подвиды, сохранена предыдущая версия ({exc})'
            )
            return result

        self._reserve_nodes(len(values), depth + 1)
        result['children'] = [
            {'name': value, 'slug': None, 'children': []}
            for value in values
        ]
        return result

    def _leaf_part_types(self, slug: str, leaf_name: str) -> list[str]:
        for attempt in range(6):
            try:
                self.call_budget.consume()
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

        if not isinstance(data, dict):
            raise AvitoTreeSyncError('Avito вернул поля узла не в виде объекта.')
        fields = data.get('fields')
        if fields is None:
            fields = []
        if not isinstance(fields, list):
            raise AvitoTreeSyncError('Avito вернул поле fields не в виде списка.')

        values: list[str] = []
        seen_values: set[str] = set()
        for field in fields:
            if not isinstance(field, dict):
                raise AvitoTreeSyncError('Avito вернул некорректный элемент fields.')
            tag = str(field.get('tag') or '')
            if not tag.endswith('SparePartType'):
                continue
            contents = field.get('content')
            if contents is None:
                contents = []
            if not isinstance(contents, list):
                raise AvitoTreeSyncError('Avito вернул content не в виде списка.')
            for content in contents:
                if not isinstance(content, dict):
                    raise AvitoTreeSyncError('Avito вернул некорректный элемент content.')
                raw_values = content.get('values')
                if raw_values is None:
                    raw_values = []
                if not isinstance(raw_values, list):
                    raise AvitoTreeSyncError('Avito вернул values не в виде списка.')
                inline = []
                for value in raw_values:
                    if not isinstance(value, dict):
                        raise AvitoTreeSyncError('Avito вернул некорректный элемент values.')
                    raw_value = value.get('value')
                    if raw_value is None or raw_value == '':
                        continue
                    if not isinstance(raw_value, str):
                        raise AvitoTreeSyncError('Значение категории Avito должно быть строкой.')
                    inline.append(raw_value)
                values_link = content.get('values_link_json')
                if values_link is not None and not isinstance(values_link, str):
                    raise AvitoTreeSyncError('Ссылка values_link_json должна быть строкой.')
                if not inline and values_link:
                    remaining = self.max_nodes - self.nodes_seen - len(values)
                    inline = self._fetch_link_values(values_link, max_values=remaining)
                for value in inline:
                    if value == leaf_name or value in seen_values:
                        continue
                    if self.nodes_seen + len(values) >= self.max_nodes:
                        raise AvitoTreeLimitExceeded(
                            f'Превышено число узлов дерева Avito: {self.max_nodes}.',
                        )
                    seen_values.add(value)
                    values.append(value)
        time.sleep(0.1)
        return values

    def _fetch_link_values(self, url: str, *, max_values: int) -> list[str]:
        if max_values < 1:
            raise AvitoTreeLimitExceeded(
                f'Превышено число узлов дерева Avito: {self.max_nodes}.',
            )
        token = self.adapter._auth.get_token(self.adapter.account)
        for attempt in range(6):
            self.call_budget.consume()
            response = _request_avito_values(url, token)
            if response.status_code == 429 and attempt < 5:
                time.sleep(3 * (attempt + 1))
                continue
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise AvitoTreeSyncError('Avito вернул некорректный JSON значений.') from exc
            if not isinstance(payload, dict):
                raise AvitoTreeSyncError('Avito вернул некорректную структуру значений.')
            raw_values = payload.get('values')
            if raw_values is None:
                return []
            if not isinstance(raw_values, list):
                raise AvitoTreeSyncError('Avito вернул values не в виде списка.')
            if len(raw_values) > max_values:
                raise AvitoTreeLimitExceeded(
                    f'Ответ values превышает остаток бюджета узлов: {max_values}.',
                )
            values = []
            for value in raw_values:
                if not isinstance(value, dict):
                    raise AvitoTreeSyncError('Avito вернул некорректный элемент values.')
                raw_value = value.get('value')
                if raw_value is None or raw_value == '':
                    continue
                if not isinstance(raw_value, str):
                    raise AvitoTreeSyncError('Значение категории Avito должно быть строкой.')
                values.append(raw_value)
            return values
        raise AvitoTreeSyncError('Avito не вернул значения поля после повторных попыток.')

    def _reserve_nodes(self, count: int, depth: int) -> None:
        if count < 0:
            raise AvitoTreeLimitExceeded('Некорректный размер дерева Avito.')
        if count and depth > self.max_depth:
            raise AvitoTreeLimitExceeded(
                f'Превышена глубина дерева Avito: {self.max_depth}.',
            )
        if self.nodes_seen + count > self.max_nodes:
            raise AvitoTreeLimitExceeded(
                f'Превышено число узлов дерева Avito: {self.max_nodes}.',
            )
        self.nodes_seen += count

    def _reserve_existing_subtree(self, nodes: list[dict], *, depth: int) -> None:
        if not isinstance(nodes, list):
            raise AvitoTreeSyncError('Резервная ветка дерева Avito должна быть списком.')
        stack = [(node, depth) for node in reversed(nodes)]
        while stack:
            node, node_depth = stack.pop()
            if not isinstance(node, dict):
                raise AvitoTreeSyncError('Резервная ветка содержит некорректный узел.')
            self._reserve_nodes(1, node_depth)
            children = node.get('children')
            if children is None:
                children = []
            if not isinstance(children, list):
                raise AvitoTreeSyncError('Резервная ветка содержит некорректное поле children.')
            stack.extend((child, node_depth + 1) for child in reversed(children))


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
        call_budget = AvitoTreeCallBudget.from_settings()
        for account in accounts:
            try:
                result = AvitoLiveTreeBuilder(
                    AvitoAdapter(account),
                    previous_tree,
                    call_budget=call_budget,
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
