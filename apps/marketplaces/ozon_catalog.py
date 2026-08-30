import hashlib
import json
from collections.abc import Mapping
from typing import Any

from cryptography.fernet import InvalidToken
from django.conf import settings

from apps.datasources.encryption import decrypt
from apps.marketplaces.adapters.ozon.client import OzonAPIError, OzonSellerClient
from apps.marketplaces.models import (
    MarketplaceAccount,
    OzonAccountProfile,
    OzonAttributeValueSnapshot,
    OzonCategoryAttributeSnapshot,
    OzonCategoryTreeSnapshot,
)
from apps.marketplaces.ozon_rollout import ozon_connection_enabled_for_account


class OzonCatalogError(RuntimeError):
    """Tenant-safe catalog error without credentials or provider response text."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


def _provider_id(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool):
        number = -1
    elif isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        number = int(value)
    else:
        number = -1
    minimum = 0 if allow_zero else 1
    if number < minimum or number > 9_223_372_036_854_775_807:
        raise OzonCatalogError(
            'schema_drift',
            f'Ozon вернул некорректное поле {field} в схеме каталога.',
        )
    return number


def _optional_provider_id(value: Any, field: str) -> int | None:
    if value in (None, ''):
        return None
    return _provider_id(value, field)


def _text(value: Any, field: str, *, maximum: int, required: bool = False) -> str:
    if value is None:
        text = ''
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise OzonCatalogError(
            'schema_drift',
            f'Ozon вернул некорректное поле {field} в схеме каталога.',
        )
    if required and not text:
        raise OzonCatalogError(
            'schema_drift',
            f'Ozon не вернул обязательное поле {field} в схеме каталога.',
        )
    return text[:maximum]


def _boolean(value: Any, field: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise OzonCatalogError(
            'schema_drift',
            f'Ozon вернул некорректное поле {field} в схеме каталога.',
        )
    return value


def normalize_category_tree(
    raw_tree: list[Any],
) -> tuple[list[dict[str, Any]], int, int]:
    """Normalize and bound recursive provider data before persistence."""
    node_count = 0
    active_type_count = 0

    def walk(
        raw_nodes: Any,
        *,
        depth: int,
        inherited_category_id: int | None,
        ancestor_disabled: bool,
    ) -> list[dict[str, Any]]:
        nonlocal node_count, active_type_count
        if depth > settings.OZON_CATALOG_MAX_DEPTH:
            raise OzonCatalogError(
                'schema_limit_exceeded',
                'Дерево категорий Ozon превысило безопасную глубину.',
            )
        if not isinstance(raw_nodes, list):
            raise OzonCatalogError(
                'schema_drift',
                'Ozon вернул некорректное дерево категорий.',
            )

        result: list[dict[str, Any]] = []
        for raw_node in raw_nodes:
            node_count += 1
            if node_count > settings.OZON_CATALOG_MAX_NODES:
                raise OzonCatalogError(
                    'schema_limit_exceeded',
                    'Дерево категорий Ozon превысило безопасный лимит узлов.',
                )
            if not isinstance(raw_node, Mapping):
                raise OzonCatalogError(
                    'schema_drift',
                    'Ozon вернул некорректный узел дерева категорий.',
                )

            own_category_id = _optional_provider_id(
                raw_node.get('description_category_id'),
                'description_category_id',
            )
            category_id = own_category_id or inherited_category_id
            type_id = _optional_provider_id(raw_node.get('type_id'), 'type_id')
            if category_id is None:
                raise OzonCatalogError(
                    'schema_drift',
                    'Тип или категория Ozon не содержит идентификатор категории.',
                )
            if type_id is None and own_category_id is None:
                raise OzonCatalogError(
                    'schema_drift',
                    'Узел дерева Ozon не содержит идентификатор категории или типа.',
                )

            disabled = _boolean(raw_node.get('disabled'), 'disabled')
            effective_disabled = ancestor_disabled or disabled
            children = walk(
                raw_node.get('children', []),
                depth=depth + 1,
                inherited_category_id=category_id,
                ancestor_disabled=effective_disabled,
            )
            normalized: dict[str, Any] = {
                'description_category_id': category_id,
                'disabled': disabled,
                'children': children,
            }
            if type_id is not None:
                if children:
                    raise OzonCatalogError(
                        'schema_drift',
                        'Ozon вернул тип товара вне последнего уровня дерева.',
                    )
                normalized.update({
                    'type_id': type_id,
                    'type_name': _text(
                        raw_node.get('type_name'),
                        'type_name',
                        maximum=500,
                        required=True,
                    ),
                })
                if not effective_disabled:
                    active_type_count += 1
            else:
                normalized['category_name'] = _text(
                    raw_node.get('category_name'),
                    'category_name',
                    maximum=500,
                    required=True,
                )
            result.append(normalized)
        return result

    normalized_tree = walk(
        raw_tree,
        depth=1,
        inherited_category_id=None,
        ancestor_disabled=False,
    )
    if not normalized_tree or active_type_count == 0:
        raise OzonCatalogError(
            'schema_drift',
            'Ozon не вернул доступные типы товаров.',
        )
    return normalized_tree, node_count, active_type_count


def normalize_category_attributes(raw_attributes: list[Any]) -> list[dict[str, Any]]:
    if not raw_attributes:
        raise OzonCatalogError(
            'schema_drift',
            'Ozon не вернул характеристики выбранной категории.',
        )
    if len(raw_attributes) > settings.OZON_CATALOG_MAX_ATTRIBUTES:
        raise OzonCatalogError(
            'schema_limit_exceeded',
            'Схема характеристик Ozon превысила безопасный лимит.',
        )

    normalized: list[dict[str, Any]] = []
    identities: set[tuple[int, int]] = set()
    for raw_attribute in raw_attributes:
        if not isinstance(raw_attribute, Mapping):
            raise OzonCatalogError(
                'schema_drift',
                'Ozon вернул некорректную характеристику категории.',
            )
        attribute_id = _provider_id(raw_attribute.get('id'), 'id')
        complex_id = _provider_id(
            raw_attribute.get('attribute_complex_id', 0),
            'attribute_complex_id',
            allow_zero=True,
        )
        identity = (attribute_id, complex_id)
        if identity in identities:
            raise OzonCatalogError(
                'schema_drift',
                'Ozon вернул повторяющуюся характеристику категории.',
            )
        identities.add(identity)
        normalized.append({
            'id': attribute_id,
            'attribute_complex_id': complex_id,
            'name': _text(
                raw_attribute.get('name'),
                'name',
                maximum=500,
                required=True,
            ),
            'description': _text(
                raw_attribute.get('description'),
                'description',
                maximum=4000,
            ),
            'type': _text(
                raw_attribute.get('type'),
                'type',
                maximum=100,
                required=True,
            ),
            'is_collection': _boolean(
                raw_attribute.get('is_collection'),
                'is_collection',
            ),
            'is_required': _boolean(
                raw_attribute.get('is_required'),
                'is_required',
            ),
            'is_aspect': _boolean(raw_attribute.get('is_aspect'), 'is_aspect'),
            'max_value_count': _provider_id(
                raw_attribute.get('max_value_count', 0),
                'max_value_count',
                allow_zero=True,
            ),
            'group_name': _text(
                raw_attribute.get('group_name'),
                'group_name',
                maximum=500,
            ),
            'group_id': _provider_id(
                raw_attribute.get('group_id', 0),
                'group_id',
                allow_zero=True,
            ),
            'dictionary_id': _provider_id(
                raw_attribute.get('dictionary_id', 0),
                'dictionary_id',
                allow_zero=True,
            ),
            'category_dependent': _boolean(
                raw_attribute.get('category_dependent'),
                'category_dependent',
            ),
            'complex_is_collection': _boolean(
                raw_attribute.get('complex_is_collection'),
                'complex_is_collection',
            ),
        })
    normalized.sort(key=lambda item: (
        item['group_id'], item['attribute_complex_id'], item['id'],
    ))
    return normalized


def normalize_attribute_values(raw_values: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_values, list):
        raise OzonCatalogError(
            'schema_drift',
            'Ozon вернул некорректный справочник характеристики.',
        )
    if len(raw_values) > settings.OZON_CATALOG_MAX_VALUES:
        raise OzonCatalogError(
            'schema_limit_exceeded',
            'Справочник характеристики Ozon превысил безопасный лимит.',
        )
    values: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for item in raw_values:
        if not isinstance(item, Mapping):
            raise OzonCatalogError(
                'schema_drift',
                'Ozon вернул некорректное значение характеристики.',
            )
        value_id = _provider_id(item.get('id'), 'id')
        if value_id in seen_ids:
            continue
        seen_ids.add(value_id)
        values.append({
            'id': value_id,
            'value': _text(
                item.get('value'),
                'value',
                maximum=1000,
                required=True,
            ),
            'info': _text(item.get('info'), 'info', maximum=1000),
            'picture': _text(item.get('picture'), 'picture', maximum=2000),
        })
    return values


def _schema_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _tree_contains_active_type(
    tree: list[dict[str, Any]],
    *,
    description_category_id: int,
    type_id: int,
) -> bool:
    return any(
        item['description_category_id'] == description_category_id
        and item['type_id'] == type_id
        for item in catalog_types_from_tree(tree)
    )


def catalog_types_from_tree(
    tree: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flatten active Ozon leaf types while preserving their category path."""
    node_count = 0
    result: list[dict[str, Any]] = []

    def walk(
        nodes: Any,
        *,
        path: list[str],
        ancestor_disabled: bool,
        depth: int,
    ) -> None:
        nonlocal node_count
        if depth > settings.OZON_CATALOG_MAX_DEPTH or not isinstance(nodes, list):
            raise OzonCatalogError(
                'schema_drift',
                'Локальный снимок дерева категорий Ozon повреждён.',
            )
        for node in nodes:
            node_count += 1
            if (
                node_count > settings.OZON_CATALOG_MAX_NODES
                or not isinstance(node, Mapping)
            ):
                raise OzonCatalogError(
                    'schema_drift',
                    'Локальный снимок дерева категорий Ozon повреждён.',
                )
            category_id = _provider_id(
                node.get('description_category_id'),
                'description_category_id',
            )
            disabled = _boolean(node.get('disabled'), 'disabled')
            effective_disabled = ancestor_disabled or disabled
            children = node.get('children')
            if not isinstance(children, list):
                raise OzonCatalogError(
                    'schema_drift',
                    'Локальный снимок дерева категорий Ozon повреждён.',
                )

            type_id = _optional_provider_id(node.get('type_id'), 'type_id')
            if type_id is not None:
                if children:
                    raise OzonCatalogError(
                        'schema_drift',
                        'Локальный снимок дерева категорий Ozon повреждён.',
                    )
                type_name = _text(
                    node.get('type_name'),
                    'type_name',
                    maximum=500,
                    required=True,
                )
                if not effective_disabled:
                    result.append({
                        'description_category_id': category_id,
                        'type_id': type_id,
                        'category_path': ' → '.join(path),
                        'type_name': type_name,
                    })
                continue

            category_name = _text(
                node.get('category_name'),
                'category_name',
                maximum=500,
                required=True,
            )
            walk(
                children,
                path=[*path, category_name],
                ancestor_disabled=effective_disabled,
                depth=depth + 1,
            )

    walk(tree, path=[], ancestor_disabled=False, depth=1)
    result.sort(key=lambda item: (
        item['category_path'].casefold(),
        item['type_name'].casefold(),
        item['type_id'],
    ))
    return result


def catalog_tree_level_from_tree(
    tree: list[dict[str, Any]],
    *,
    parent_ids: tuple[int, ...] = (),
) -> dict[str, list[dict[str, Any]]]:
    """Return one active local tree level for a tenant-facing category picker."""

    nodes = tree
    path: list[dict[str, Any]] = []
    for parent_id in parent_ids:
        matches = [
            node for node in nodes
            if (
                node.get('type_id') is None
                and node.get('description_category_id') == parent_id
                and node.get('disabled') is False
            )
        ]
        if len(matches) != 1:
            raise OzonCatalogError(
                'invalid_parent_path',
                'Выбранный раздел отсутствует в актуальном дереве Ozon.',
            )
        parent = matches[0]
        path.append({
            'description_category_id': parent_id,
            'name': parent['category_name'],
        })
        nodes = parent['children']

    def has_active_type(node: Mapping[str, Any], *, depth: int) -> bool:
        if depth > settings.OZON_CATALOG_MAX_DEPTH or node.get('disabled') is not False:
            return False
        if node.get('type_id') is not None:
            return True
        children = node.get('children')
        return isinstance(children, list) and any(
            isinstance(child, Mapping) and has_active_type(child, depth=depth + 1)
            for child in children
        )

    options: list[dict[str, Any]] = []
    category_path = ' → '.join(item['name'] for item in path)
    for node in nodes:
        if not isinstance(node, Mapping) or not has_active_type(node, depth=1):
            continue
        type_id = node.get('type_id')
        if type_id is None:
            name = node['category_name']
            options.append({
                'kind': 'category',
                'description_category_id': node['description_category_id'],
                'type_id': None,
                'name': name,
                'category_path': ' → '.join(filter(None, (category_path, name))),
            })
        else:
            options.append({
                'kind': 'type',
                'description_category_id': node['description_category_id'],
                'type_id': type_id,
                'name': node['type_name'],
                'category_path': category_path,
            })
    options.sort(key=lambda item: (
        item['kind'] == 'type',
        item['name'].casefold(),
        item['description_category_id'],
        item['type_id'] or 0,
    ))
    return {'path': path, 'options': options}


def _touch(snapshot) -> None:
    snapshot.save(update_fields=['updated_at'])


def _tree_metadata(snapshot: OzonCategoryTreeSnapshot | None) -> dict | None:
    if snapshot is None:
        return None
    return {
        'revision': snapshot.schema_hash,
        'language': snapshot.language,
        'node_count': snapshot.node_count,
        'active_type_count': snapshot.active_type_count,
        'first_synced_at': snapshot.created_at,
        'last_checked_at': snapshot.updated_at,
    }


def _attribute_metadata(
    snapshot: OzonCategoryAttributeSnapshot | None,
) -> dict | None:
    if snapshot is None:
        return None
    return {
        'revision': snapshot.schema_hash,
        'description_category_id': snapshot.description_category_id,
        'type_id': snapshot.type_id,
        'language': snapshot.language,
        'attribute_count': snapshot.attribute_count,
        'required_attribute_count': snapshot.required_attribute_count,
        'first_synced_at': snapshot.created_at,
        'last_checked_at': snapshot.updated_at,
    }


class OzonCatalogService:
    """Manual, account-scoped catalog reads with no Avito runtime dependency."""

    @staticmethod
    def _require_access(account: MarketplaceAccount, confirmed: bool) -> None:
        if account.marketplace != MarketplaceAccount.MARKETPLACE_OZON:
            raise OzonCatalogError(
                'wrong_provider',
                'Справочник Ozon доступен только для аккаунта Ozon.',
            )
        if not account.is_active or not OzonAccountProfile.objects.filter(
            account=account,
        ).exists():
            raise OzonCatalogError(
                'account_not_ready',
                'Сначала подключите и проверьте аккаунт Ozon.',
            )
        if not ozon_connection_enabled_for_account(
            account.tenant,
            account.external_id,
        ):
            raise OzonCatalogError(
                'provider_disabled',
                'Read-only справочник Ozon закрыт для этого аккаунта rollout-настройками.',
            )
        if confirmed is not True:
            raise OzonCatalogError(
                'confirmation_required',
                'Подтвердите read-only обновление справочника Ozon.',
            )

    @staticmethod
    def _client(account: MarketplaceAccount) -> OzonSellerClient:
        try:
            credentials = decrypt(bytes(account.credentials_enc))
        except (InvalidToken, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise OzonCatalogError(
                'invalid_credentials',
                'Не удалось прочитать сохранённые credentials Ozon.',
            ) from exc
        client_id = str(credentials.get('client_id') or '').strip()
        api_key = str(credentials.get('api_key') or '').strip()
        if client_id != account.external_id or not api_key:
            raise OzonCatalogError(
                'invalid_credentials',
                'Сохранённые credentials не соответствуют аккаунту Ozon.',
            )
        return OzonSellerClient(client_id=client_id, api_key=api_key)

    @staticmethod
    def _provider_error(exc: OzonAPIError) -> OzonCatalogError:
        return OzonCatalogError(
            exc.code,
            str(exc),
            retry_after_seconds=exc.retry_after_seconds,
        )

    @classmethod
    def sync_tree(
        cls,
        account: MarketplaceAccount,
        *,
        language: str,
        confirmed: bool,
    ) -> OzonCategoryTreeSnapshot:
        cls._require_access(account, confirmed)
        try:
            raw_tree = cls._client(account).get_description_category_tree(
                language=language,
            )
        except OzonAPIError as exc:
            raise cls._provider_error(exc) from exc
        tree, node_count, active_type_count = normalize_category_tree(raw_tree)
        snapshot, created = OzonCategoryTreeSnapshot.objects.get_or_create(
            account=account,
            language=language,
            schema_hash=_schema_hash(tree),
            defaults={
                'tree': tree,
                'node_count': node_count,
                'active_type_count': active_type_count,
            },
        )
        if not created:
            _touch(snapshot)
        return snapshot

    @classmethod
    def sync_attributes(
        cls,
        account: MarketplaceAccount,
        *,
        description_category_id: int,
        type_id: int,
        language: str,
        confirmed: bool,
    ) -> OzonCategoryAttributeSnapshot:
        cls._require_access(account, confirmed)
        tree_snapshot = OzonCategoryTreeSnapshot.objects.filter(
            account=account,
            language=language,
        ).order_by('-updated_at', '-pk').first()
        if tree_snapshot is None:
            raise OzonCatalogError(
                'tree_required',
                'Сначала обновите дерево категорий Ozon.',
            )
        if not _tree_contains_active_type(
            tree_snapshot.tree,
            description_category_id=description_category_id,
            type_id=type_id,
        ):
            raise OzonCatalogError(
                'invalid_category_type',
                'Выбранная пара категории и типа отсутствует в актуальном дереве Ozon.',
            )
        try:
            raw_attributes = cls._client(
                account,
            ).get_description_category_attributes(
                description_category_id=description_category_id,
                type_id=type_id,
                language=language,
            )
        except OzonAPIError as exc:
            raise cls._provider_error(exc) from exc
        attributes = normalize_category_attributes(raw_attributes)
        snapshot, created = OzonCategoryAttributeSnapshot.objects.get_or_create(
            account=account,
            description_category_id=description_category_id,
            type_id=type_id,
            language=language,
            schema_hash=_schema_hash(attributes),
            defaults={
                'attributes': attributes,
                'attribute_count': len(attributes),
                'required_attribute_count': sum(
                    1 for attribute in attributes if attribute['is_required']
                ),
            },
        )
        if not created:
            _touch(snapshot)
        return snapshot

    @classmethod
    def search_attribute_values(
        cls,
        account: MarketplaceAccount,
        *,
        description_category_id: int,
        type_id: int,
        attribute_id: int,
        query: str,
        language: str,
        confirmed: bool,
    ) -> OzonAttributeValueSnapshot:
        cls._require_access(account, confirmed)
        schema = OzonCategoryAttributeSnapshot.objects.filter(
            account=account,
            description_category_id=description_category_id,
            type_id=type_id,
            language=language,
        ).order_by('-updated_at', '-pk').first()
        if schema is None:
            raise OzonCatalogError(
                'attribute_schema_required',
                'Сначала загрузите характеристики выбранной категории Ozon.',
            )
        attribute = next(
            (
                item for item in schema.attributes
                if item['id'] == attribute_id and item['dictionary_id'] > 0
            ),
            None,
        )
        if attribute is None:
            raise OzonCatalogError(
                'invalid_dictionary_attribute',
                'Выбранная характеристика не использует справочник Ozon.',
            )
        normalized_query = query.strip()
        if len(normalized_query) < 2:
            raise OzonCatalogError(
                'query_too_short',
                'Введите минимум два символа для поиска.',
            )
        try:
            raw_values = cls._client(
                account,
            ).search_description_category_attribute_values(
                description_category_id=description_category_id,
                type_id=type_id,
                attribute_id=attribute_id,
                value=normalized_query,
            )
        except OzonAPIError as exc:
            raise cls._provider_error(exc) from exc
        values = normalize_attribute_values(raw_values)
        snapshot, created = OzonAttributeValueSnapshot.objects.get_or_create(
            account=account,
            description_category_id=description_category_id,
            type_id=type_id,
            attribute_id=attribute_id,
            language=language,
            query=normalized_query,
            attribute_schema_hash=schema.schema_hash,
            schema_hash=_schema_hash(values),
            defaults={'values': values, 'value_count': len(values)},
        )
        if not created:
            _touch(snapshot)
        return snapshot

    @staticmethod
    def state(account: MarketplaceAccount, *, language: str = 'DEFAULT') -> dict:
        tree = OzonCategoryTreeSnapshot.objects.filter(
            account=account,
            language=language,
        ).order_by('-updated_at', '-pk').first()
        attribute_snapshots = OzonCategoryAttributeSnapshot.objects.filter(
            account=account,
            language=language,
        )
        latest_attribute = attribute_snapshots.order_by(
            '-updated_at', '-pk',
        ).first()
        schema_count = attribute_snapshots.values(
            'description_category_id', 'type_id',
        ).distinct().count()
        return {
            'account_id': account.pk,
            'marketplace': MarketplaceAccount.MARKETPLACE_OZON,
            'tree': _tree_metadata(tree),
            'attribute_schema_count': schema_count,
            'latest_attribute_schema': _attribute_metadata(latest_attribute),
        }

    @staticmethod
    def category_types(
        account: MarketplaceAccount,
        *,
        language: str = 'DEFAULT',
        search: str = '',
    ) -> tuple[OzonCategoryTreeSnapshot | None, list[dict[str, Any]]]:
        """Read active types from the latest local snapshot; never call Ozon."""
        snapshot = OzonCategoryTreeSnapshot.objects.filter(
            account=account,
            language=language,
        ).order_by('-updated_at', '-pk').first()
        if snapshot is None:
            return None, []
        category_types = catalog_types_from_tree(snapshot.tree)
        query = search.strip().casefold()
        if query:
            category_types = [
                item for item in category_types
                if query in ' '.join((
                    item['category_path'],
                    item['type_name'],
                    str(item['description_category_id']),
                    str(item['type_id']),
                )).casefold()
            ]
        return snapshot, category_types

    @staticmethod
    def category_tree_level(
        account: MarketplaceAccount,
        *,
        language: str = 'DEFAULT',
        parent_ids: tuple[int, ...] = (),
    ) -> tuple[OzonCategoryTreeSnapshot | None, dict[str, list[dict[str, Any]]]]:
        """Browse one local tree level without calling the Ozon provider."""

        snapshot = OzonCategoryTreeSnapshot.objects.filter(
            account=account,
            language=language,
        ).order_by('-updated_at', '-pk').first()
        if snapshot is None:
            return None, {'path': [], 'options': []}
        return snapshot, catalog_tree_level_from_tree(
            snapshot.tree,
            parent_ids=parent_ids,
        )


__all__ = [
    'OzonCatalogError',
    'OzonCatalogService',
    'catalog_tree_level_from_tree',
    'catalog_types_from_tree',
    'normalize_attribute_values',
    'normalize_category_attributes',
    'normalize_category_tree',
]
