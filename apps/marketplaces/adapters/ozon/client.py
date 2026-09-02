from collections.abc import Mapping
from dataclasses import dataclass
import datetime
from typing import Any

import requests
from django.conf import settings
from django.utils.dateparse import parse_datetime

from apps.core.http_responses import bounded_http_request


OZON_API_BASE_URL = 'https://api-seller.ozon.ru'


class OzonAPIError(RuntimeError):
    """Safe provider error which never includes credentials or response bodies."""

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


@dataclass(frozen=True)
class OzonWarehouse:
    warehouse_id: str
    name: str


@dataclass(frozen=True)
class OzonConnectionSnapshot:
    company_name: str
    seller_name: str
    currency: str
    roles: tuple[str, ...]
    warehouses: tuple[OzonWarehouse, ...]
    api_key_expires_at: datetime.datetime | None = None
    api_methods: tuple[str, ...] = ()


class OzonSellerClient:
    """Bounded Ozon client; mutations are exposed as explicit methods."""

    def __init__(
        self,
        *,
        client_id: str,
        api_key: str,
        session: requests.Session | None = None,
    ):
        self._client_id = str(client_id).strip()
        self._api_key = str(api_key).strip()
        self._session = session or requests.Session()

    def verify_connection(self) -> OzonConnectionSnapshot:
        roles, api_methods, api_key_expires_at = self._get_roles_info()
        seller_info = self.get_seller_info()
        warehouses = self.list_warehouses()
        company = _mapping(seller_info.get('company'))
        seller = _mapping(seller_info.get('seller'))
        return OzonConnectionSnapshot(
            company_name=_first_text(
                company,
                'name',
                'company_name',
                'legal_name',
            ),
            seller_name=_first_text(
                seller,
                'name',
                'seller_name',
                'display_name',
            ),
            currency=_first_text(company, 'currency')[:10],
            roles=tuple(roles),
            warehouses=tuple(warehouses),
            api_key_expires_at=api_key_expires_at,
            api_methods=tuple(api_methods),
        )

    def get_roles(self) -> list[str]:
        roles, _, _ = self._get_roles_info()
        return roles

    def _get_roles_info(
        self,
    ) -> tuple[list[str], list[str], datetime.datetime | None]:
        payload = self._post('/v1/roles', {})
        raw_roles = payload.get('roles')
        result = payload.get('result')
        if raw_roles is None and isinstance(result, list):
            raw_roles = result
        elif raw_roles is None:
            raw_roles = _mapping(result).get('roles', [])
        if not isinstance(raw_roles, list):
            raise OzonAPIError('invalid_response', 'Ozon вернул некорректный список ролей.')
        if len(raw_roles) > 100:
            raise OzonAPIError(
                'invalid_response',
                'Список ролей Ozon превысил безопасный лимит.',
            )

        roles: set[str] = set()
        methods: set[str] = set()
        for item in raw_roles:
            if isinstance(item, str):
                value = item.strip()
            elif isinstance(item, Mapping):
                value = _first_text(item, 'name', 'role', 'code')
            else:
                value = ''
            if value:
                roles.add(value[:100])
            if isinstance(item, Mapping):
                methods.update(_method_names(item.get('methods')))
        methods.update(_method_names(payload.get('methods')))
        if isinstance(result, Mapping):
            methods.update(_method_names(result.get('methods')))
        if len(methods) > 500:
            raise OzonAPIError(
                'invalid_response',
                'Список методов Ozon превысил безопасный лимит.',
            )
        expires_at = payload.get('expires_at')
        if expires_at is None and isinstance(result, Mapping):
            expires_at = result.get('expires_at')
        return sorted(roles), sorted(methods), _parse_provider_datetime(expires_at)

    def get_seller_info(self) -> dict[str, Any]:
        payload = self._post('/v1/seller/info', {})
        result = payload.get('result')
        if isinstance(result, Mapping):
            return dict(result)
        return payload

    def list_warehouses(self) -> list[OzonWarehouse]:
        warehouses: dict[str, OzonWarehouse] = {}
        cursor = ''
        seen_cursors: set[str] = set()

        for _ in range(settings.OZON_API_MAX_PAGES):
            request_payload: dict[str, Any] = {'limit': 100}
            if cursor:
                request_payload['cursor'] = cursor
            payload = self._post('/v2/warehouse/list', request_payload)
            raw_items = payload.get('warehouses')
            result = payload.get('result')
            if raw_items is None and isinstance(result, list):
                raw_items = result
            elif raw_items is None and isinstance(result, Mapping):
                raw_items = result.get('warehouses')
            if not isinstance(raw_items, list):
                raise OzonAPIError(
                    'invalid_response',
                    'Ozon вернул некорректный список складов.',
                )

            for item in raw_items:
                if not isinstance(item, Mapping):
                    continue
                warehouse_id = _first_text(item, 'warehouse_id', 'id')
                if not warehouse_id:
                    continue
                warehouses[warehouse_id] = OzonWarehouse(
                    warehouse_id=warehouse_id[:100],
                    name=_first_text(item, 'name', 'warehouse_name')[:300],
                )

            has_next = bool(payload.get('has_next'))
            next_cursor = str(payload.get('cursor') or '').strip()
            if isinstance(result, Mapping):
                has_next = bool(result.get('has_next', has_next))
                next_cursor = str(result.get('cursor') or next_cursor).strip()
            if not has_next:
                return list(warehouses.values())
            if not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
                raise OzonAPIError(
                    'invalid_response',
                    'Ozon вернул некорректный курсор списка складов.',
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        raise OzonAPIError(
            'page_limit_exceeded',
            'Список складов Ozon превысил безопасный лимит страниц.',
        )

    def get_description_category_tree(
        self,
        *,
        language: str = 'DEFAULT',
    ) -> list[dict[str, Any]]:
        payload = self._post(
            '/v1/description-category/tree',
            {'language': language},
            max_bytes=settings.OZON_CATALOG_RESPONSE_MAX_BYTES,
        )
        result = payload.get('result')
        if not isinstance(result, list):
            raise OzonAPIError(
                'invalid_response',
                'Ozon вернул некорректное дерево категорий.',
            )
        return result

    def get_description_category_attributes(
        self,
        *,
        description_category_id: int,
        type_id: int,
        language: str = 'DEFAULT',
    ) -> list[dict[str, Any]]:
        payload = self._post(
            '/v1/description-category/attribute',
            {
                'description_category_id': description_category_id,
                'type_id': type_id,
                'language': language,
            },
            max_bytes=settings.OZON_CATALOG_RESPONSE_MAX_BYTES,
        )
        result = payload.get('result')
        if not isinstance(result, list):
            raise OzonAPIError(
                'invalid_response',
                'Ozon вернул некорректную схему характеристик.',
            )
        return result

    def search_description_category_attribute_values(
        self,
        *,
        description_category_id: int,
        type_id: int,
        attribute_id: int,
        value: str,
    ) -> list[dict[str, Any]]:
        payload = self._post(
            '/v1/description-category/attribute/values/search',
            {
                'description_category_id': description_category_id,
                'type_id': type_id,
                'attribute_id': attribute_id,
                'value': value,
                'limit': 100,
            },
            max_bytes=settings.OZON_CATALOG_RESPONSE_MAX_BYTES,
        )
        result = payload.get('result')
        if isinstance(result, Mapping):
            result = result.get('values')
        if not isinstance(result, list):
            raise OzonAPIError(
                'invalid_response',
                'Ozon вернул некорректный справочник характеристики.',
            )
        return result

    def import_products(self, items: list[dict[str, Any]]) -> str:
        """Create/update at most 100 products and return the provider task ID."""

        if not items or len(items) > 100:
            raise ValueError('Ozon product import accepts from 1 to 100 items.')
        payload = self._post('/v3/product/import', {'items': items})
        result = payload.get('result')
        task_id = payload.get('task_id')
        if task_id is None and isinstance(result, Mapping):
            task_id = result.get('task_id')
        task_id = str(task_id or '').strip()
        if not task_id or len(task_id) > 100:
            raise OzonAPIError(
                'invalid_response',
                'Ozon не вернул идентификатор задачи импорта.',
            )
        return task_id

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        url = f'{OZON_API_BASE_URL}{path}'
        try:
            response = bounded_http_request(
                self._session.post,
                url,
                json=payload,
                headers={
                    'Client-Id': self._client_id,
                    'Api-Key': self._api_key,
                    'Content-Type': 'application/json',
                },
                timeout=settings.OZON_API_TIMEOUT_SECONDS,
                max_elapsed_seconds=settings.OZON_API_TIMEOUT_SECONDS,
                max_bytes=max_bytes or settings.OZON_API_RESPONSE_MAX_BYTES,
            )
        except (requests.RequestException, ValueError) as exc:
            raise OzonAPIError(
                'connection_error',
                'Не удалось связаться с Ozon Seller API.',
            ) from exc

        status_code = int(response.status_code)
        if status_code in {401, 403}:
            raise OzonAPIError(
                'invalid_credentials',
                'Ozon отклонил Client-Id или API-ключ.',
            )
        if status_code == 429:
            raise OzonAPIError(
                'rate_limited',
                'Ozon временно ограничил частоту запросов.',
                retry_after_seconds=_retry_after(response.headers.get('Retry-After')),
            )
        if status_code >= 500:
            raise OzonAPIError(
                'provider_unavailable',
                'Ozon Seller API временно недоступен.',
            )
        if status_code < 200 or status_code >= 300:
            raise OzonAPIError(
                'request_rejected',
                'Ozon отклонил запрос.',
            )
        try:
            data = response.json()
        except (TypeError, ValueError) as exc:
            raise OzonAPIError(
                'invalid_response',
                'Ozon вернул некорректный ответ.',
            ) from exc
        if not isinstance(data, dict):
            raise OzonAPIError(
                'invalid_response',
                'Ozon вернул некорректный ответ.',
            )
        return data


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(value: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        item = value.get(key)
        if item is not None:
            text = str(item).strip()
            if text:
                return text
    return ''


def _retry_after(value: Any) -> int | None:
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return min(max(seconds, 0), 3600)


def _method_names(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    methods: set[str] = set()
    for item in value:
        if isinstance(item, str):
            method = item.strip()
        elif isinstance(item, Mapping):
            method = _first_text(item, 'name', 'method', 'path')
        else:
            method = ''
        if method:
            methods.add(method[:200])
    return methods


def _parse_provider_datetime(value: Any) -> datetime.datetime | None:
    if value in (None, ''):
        return None
    parsed = parse_datetime(str(value).strip())
    if parsed is None:
        raise OzonAPIError(
            'invalid_response',
            'Ozon вернул некорректный срок действия API-ключа.',
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed
