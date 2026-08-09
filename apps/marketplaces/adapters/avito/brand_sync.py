"""Загрузка и безопасное обновление справочника Brand из Avito."""
from __future__ import annotations

import requests
from django.conf import settings
from django.db import transaction
from django.utils.timezone import now

from apps.core.http_responses import bounded_http_request
from apps.marketplaces.adapters.avito.adapter import AVITO_API_BASE, AvitoAdapter
from apps.marketplaces.models import AvitoBrandCatalog, MarketplaceAccount

SOURCE_NODE = 'transmissiia_i_privod'
BRAND_FIELD_ID = 110548
MIN_CATALOG_SIZE = 100
MAX_SHRINK_RATIO = 0.20


class BrandCatalogSyncError(RuntimeError):
    """Новая версия не может безопасно заменить рабочий справочник."""


def fetch_avito_brands(account: MarketplaceAccount | None = None) -> list[str]:
    account = account or MarketplaceAccount.objects.filter(is_active=True).first()
    if account is None:
        raise BrandCatalogSyncError('Нет активного аккаунта Avito для запроса API')
    token = AvitoAdapter(account)._auth.get_token(account)
    response = bounded_http_request(
        requests.get,
        f'{AVITO_API_BASE}/autoload/v1/user-docs/node/{SOURCE_NODE}/field/{BRAND_FIELD_ID}/values-json',
        headers={'Authorization': f'Bearer {token}'},
        timeout=60,
        max_bytes=settings.AVITO_API_RESPONSE_MAX_BYTES,
    )
    response.raise_for_status()
    payload = response.json()
    values = payload if isinstance(payload, list) else payload.get('values', [])
    return sorted({
        str(item.get('value') if isinstance(item, dict) else item).strip()
        for item in values if item and (not isinstance(item, dict) or item.get('value'))
    })


def validate_catalog(brands: list[str], previous_count: int = 0) -> None:
    if len(brands) < MIN_CATALOG_SIZE:
        raise BrandCatalogSyncError(
            f'Avito вернул аномально маленький справочник: {len(brands)} значений'
        )
    minimum_expected = int(previous_count * (1 - MAX_SHRINK_RATIO))
    if previous_count and len(brands) < minimum_expected:
        raise BrandCatalogSyncError(
            f'Размер справочника аномально уменьшился: {previous_count} → {len(brands)}'
        )


def sync_brand_catalog(account: MarketplaceAccount | None = None) -> AvitoBrandCatalog:
    """Атомарно заменяет каталог только после полной загрузки и проверки."""
    brands = fetch_avito_brands(account)
    previous = AvitoBrandCatalog.objects.filter(pk=1).only('brands').first()
    validate_catalog(brands, len(previous.brands) if previous else 0)
    with transaction.atomic():
        catalog, _ = AvitoBrandCatalog.objects.update_or_create(
            pk=1,
            defaults={
                'source_node': SOURCE_NODE,
                'field_id': BRAND_FIELD_ID,
                'brands': brands,
                'synced_at': now(),
            },
        )
    from apps.marketplaces.adapters.avito.brand_catalog import clear_brand_catalog_cache
    clear_brand_catalog_cache()
    return catalog
