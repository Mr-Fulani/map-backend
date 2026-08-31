from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TypedDict

from django.db import transaction

from apps.marketplaces.models import (
    MarketplaceAccount,
    OzonCategoryPolicy,
    OzonCategoryTreeSnapshot,
)


class OzonCategoryPolicyError(RuntimeError):
    """Tenant-safe local policy error with no provider response data."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class CategoryPolicyChanges(TypedDict, total=False):
    enabled_override: bool | None
    margin_pct: Decimal | None


@dataclass(frozen=True)
class ResolvedOzonCategoryNode:
    description_category_id: int
    type_id: int | None
    category_ids: tuple[int, ...]
    category_names: tuple[str, ...]
    node_name: str

    @property
    def category_path(self) -> str:
        return ' → '.join(self.category_names)


def latest_tree_snapshot(
    account: MarketplaceAccount,
    *,
    language: str = OzonCategoryTreeSnapshot.LANGUAGE_DEFAULT,
) -> OzonCategoryTreeSnapshot | None:
    return OzonCategoryTreeSnapshot.objects.filter(
        account=account,
        language=language,
    ).order_by('-updated_at', '-pk').first()


def _has_active_type(node: dict[str, Any]) -> bool:
    if node.get('disabled') is not False:
        return False
    if node.get('type_id') is not None:
        return True
    return any(
        isinstance(child, dict) and _has_active_type(child)
        for child in node.get('children', [])
    )


def resolve_category_node(
    snapshot: OzonCategoryTreeSnapshot,
    *,
    description_category_id: int,
    type_id: int | None,
    category_path_ids: tuple[int, ...],
) -> ResolvedOzonCategoryNode:
    """Resolve one active node through an exact category path in a local tree."""

    if not category_path_ids or category_path_ids[-1] != description_category_id:
        raise OzonCategoryPolicyError(
            'invalid_category_path',
            'Выбранный путь не соответствует категории Ozon.',
        )

    nodes = snapshot.tree
    names: list[str] = []
    selected_category: dict[str, Any] | None = None
    for category_id in category_path_ids:
        matches = [
            node for node in nodes
            if (
                isinstance(node, dict)
                and node.get('type_id') is None
                and node.get('description_category_id') == category_id
                and node.get('disabled') is False
            )
        ]
        if len(matches) != 1:
            raise OzonCategoryPolicyError(
                'invalid_category_path',
                'Выбранная ветка отсутствует в актуальном дереве Ozon.',
            )
        selected_category = matches[0]
        name = selected_category.get('category_name')
        if not isinstance(name, str) or not name:
            raise OzonCategoryPolicyError(
                'invalid_category_path',
                'Локальный снимок Ozon содержит некорректную категорию.',
            )
        names.append(name)
        children = selected_category.get('children')
        nodes = children if isinstance(children, list) else []

    if selected_category is None or not _has_active_type(selected_category):
        raise OzonCategoryPolicyError(
            'inactive_category',
            'В выбранной категории Ozon нет доступных типов товаров.',
        )

    node_name = names[-1]
    if type_id is not None:
        matches = [
            node for node in nodes
            if (
                isinstance(node, dict)
                and node.get('description_category_id') == description_category_id
                and node.get('type_id') == type_id
                and node.get('disabled') is False
            )
        ]
        if len(matches) != 1:
            raise OzonCategoryPolicyError(
                'invalid_category_type',
                'Выбранный тип отсутствует в актуальном дереве Ozon.',
            )
        type_name = matches[0].get('type_name')
        if not isinstance(type_name, str) or not type_name:
            raise OzonCategoryPolicyError(
                'invalid_category_type',
                'Локальный снимок Ozon содержит некорректный тип товара.',
            )
        node_name = type_name

    return ResolvedOzonCategoryNode(
        description_category_id=description_category_id,
        type_id=type_id,
        category_ids=category_path_ids,
        category_names=tuple(names),
        node_name=node_name,
    )


def _policy_identity(
    description_category_id: int,
    type_id: int | None,
) -> tuple[int, int | None]:
    return description_category_id, type_id


def _source_presentation(policy: OzonCategoryPolicy | None) -> dict | None:
    if policy is None:
        return None
    return {
        'description_category_id': policy.description_category_id,
        'type_id': policy.type_id,
        'name': policy.node_name,
        'category_path': policy.category_path,
    }


def category_policy_presentation(policy: OzonCategoryPolicy) -> dict:
    return {
        'id': policy.pk,
        'description_category_id': policy.description_category_id,
        'type_id': policy.type_id,
        'enabled_override': policy.enabled_override,
        'margin_pct': (
            str(policy.margin_pct) if policy.margin_pct is not None else None
        ),
        'category_path': policy.category_path,
        'node_name': policy.node_name,
        'tree_revision': policy.tree_revision,
        'updated_at': policy.updated_at,
    }


def effective_policy_state(
    *,
    policies: dict[tuple[int, int | None], OzonCategoryPolicy],
    category_ids: tuple[int, ...],
    description_category_id: int,
    type_id: int | None,
) -> dict:
    enabled = True
    margin = Decimal('0')
    enabled_source: OzonCategoryPolicy | None = None
    margin_source: OzonCategoryPolicy | None = None

    identities = [_policy_identity(category_id, None) for category_id in category_ids]
    if type_id is not None:
        identities.append(_policy_identity(description_category_id, type_id))

    for identity in identities:
        policy = policies.get(identity)
        if policy is None:
            continue
        if policy.enabled_override is not None:
            enabled = policy.enabled_override
            enabled_source = policy
        if policy.margin_pct is not None:
            margin = policy.margin_pct
            margin_source = policy

    own_policy = policies.get(_policy_identity(description_category_id, type_id))
    return {
        'enabled_override': (
            own_policy.enabled_override if own_policy is not None else None
        ),
        'effective_enabled': enabled,
        'enabled_source': _source_presentation(enabled_source),
        'margin_pct': (
            str(own_policy.margin_pct)
            if own_policy is not None and own_policy.margin_pct is not None
            else None
        ),
        'effective_margin_pct': str(margin),
        'margin_source': _source_presentation(margin_source),
    }


def decorate_tree_level_with_policies(
    account: MarketplaceAccount,
    level: dict[str, list[dict[str, Any]]],
    *,
    parent_ids: tuple[int, ...],
) -> dict[str, list[dict[str, Any]]]:
    """Attach local effective settings without mutating the provider snapshot."""

    policies = {
        _policy_identity(policy.description_category_id, policy.type_id): policy
        for policy in OzonCategoryPolicy.objects.filter(
            tenant=account.tenant,
            account=account,
        )
    }
    options: list[dict[str, Any]] = []
    for option in level['options']:
        category_id = option['description_category_id']
        type_id = option['type_id']
        if option['kind'] == 'category':
            category_ids = (*parent_ids, category_id)
        elif parent_ids and parent_ids[-1] == category_id:
            category_ids = parent_ids
        else:
            category_ids = (*parent_ids, category_id)
        options.append({
            **option,
            'policy': effective_policy_state(
                policies=policies,
                category_ids=category_ids,
                description_category_id=category_id,
                type_id=type_id,
            ),
        })
    return {'path': level['path'], 'options': options}


def update_category_policy(
    account: MarketplaceAccount,
    *,
    description_category_id: int,
    type_id: int | None,
    category_path_ids: tuple[int, ...],
    expected_tree_revision: str,
    changes: CategoryPolicyChanges,
) -> dict:
    """Upsert or clear one sparse policy row for an exact Ozon account."""

    if account.marketplace != MarketplaceAccount.MARKETPLACE_OZON:
        raise OzonCategoryPolicyError(
            'wrong_provider',
            'Настройки категорий доступны только для аккаунта Ozon.',
        )
    snapshot = latest_tree_snapshot(account)
    if snapshot is None:
        raise OzonCategoryPolicyError(
            'tree_required',
            'Сначала загрузите дерево категорий Ozon.',
        )
    if expected_tree_revision != snapshot.schema_hash:
        raise OzonCategoryPolicyError(
            'tree_revision_outdated',
            'Дерево Ozon обновилось — откройте категорию заново.',
        )
    node = resolve_category_node(
        snapshot,
        description_category_id=description_category_id,
        type_id=type_id,
        category_path_ids=category_path_ids,
    )

    lookup = {
        'tenant': account.tenant,
        'account': account,
        'description_category_id': description_category_id,
        'type_id': type_id,
    }
    with transaction.atomic():
        MarketplaceAccount.objects.select_for_update().get(pk=account.pk)
        policy = OzonCategoryPolicy.objects.select_for_update().filter(**lookup).first()
        enabled_override = policy.enabled_override if policy is not None else None
        margin_pct = policy.margin_pct if policy is not None else None
        if 'enabled_override' in changes:
            enabled_override = changes['enabled_override']
        if 'margin_pct' in changes:
            margin_pct = changes['margin_pct']

        if enabled_override is None and margin_pct is None:
            if policy is not None:
                policy.delete()
            policy = None
        elif policy is None:
            policy = OzonCategoryPolicy.objects.create(
                **lookup,
                enabled_override=enabled_override,
                margin_pct=margin_pct,
                category_path=node.category_path,
                node_name=node.node_name,
                tree_revision=snapshot.schema_hash,
            )
        else:
            policy.enabled_override = enabled_override
            policy.margin_pct = margin_pct
            policy.category_path = node.category_path
            policy.node_name = node.node_name
            policy.tree_revision = snapshot.schema_hash
            policy.save(update_fields=[
                'enabled_override',
                'margin_pct',
                'category_path',
                'node_name',
                'tree_revision',
                'updated_at',
            ])

    policies = {
        _policy_identity(item.description_category_id, item.type_id): item
        for item in OzonCategoryPolicy.objects.filter(
            tenant=account.tenant,
            account=account,
        )
    }
    return {
        'node': {
            'description_category_id': node.description_category_id,
            'type_id': node.type_id,
            'category_path_ids': list(node.category_ids),
            'category_path': node.category_path,
            'name': node.node_name,
        },
        'stored_policy': (
            category_policy_presentation(policy) if policy is not None else None
        ),
        'policy': effective_policy_state(
            policies=policies,
            category_ids=node.category_ids,
            description_category_id=node.description_category_id,
            type_id=node.type_id,
        ),
    }


__all__ = [
    'CategoryPolicyChanges',
    'OzonCategoryPolicyError',
    'decorate_tree_level_with_policies',
    'effective_policy_state',
    'resolve_category_node',
    'update_category_policy',
]
