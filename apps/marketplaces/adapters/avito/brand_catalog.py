"""Каталог брендов Avito (поле Brand / «Производитель» для запчастей).

Avito принимает в Brand только значения из своего каталога (для новых
запчастей поле обязательно). Написание Avito нормализует сам (регистр,
пунктуация: «HYUNDAI / KIA» → «Hyundai-KIA»), поэтому проверяем нечётко —
по нормализованной форме. Незнакомый бренд — почти гарантированное
отклонение «Производитель. Значение не найдено».

Источник — data/avito_brand_catalog.json, обновляется командой
sync_avito_brand_catalog. Без файла проверка отключается (fail-open):
лучше пропустить в фид, чем ложно завернуть.
"""
import difflib
import json
from datetime import timedelta
from pathlib import Path

from django.core.cache import cache
from django.utils.timezone import now

_CATALOG_PATH = Path(__file__).resolve().parents[2] / 'data' / 'avito_brand_catalog.json'
_CACHE_KEY = 'avito:brand_catalog:normalized:v2'
CATALOG_MAX_AGE = timedelta(days=3)


def normalize_brand_name(name: str) -> str:
    """Нормализует бренд для сравнения: нижний регистр, только буквы/цифры."""
    return ''.join(char for char in str(name or '').lower() if char.isalnum())


def _catalog_by_normalized_name() -> dict[str, str]:
    """{нормализованное имя: каноничное написание Avito}; БД → JSON fallback."""
    cached = cache.get(_CACHE_KEY)
    if cached is not None:
        return cached
    brands = None
    try:
        from apps.marketplaces.models import AvitoBrandCatalog
        state = AvitoBrandCatalog.objects.filter(pk=1).only('brands').first()
        if state:
            brands = state.brands
    except Exception:
        # Миграции/management-команды могут обращаться к модулю до создания таблицы.
        brands = None
    if brands is None:
        try:
            data = json.loads(_CATALOG_PATH.read_text(encoding='utf-8'))
            brands = data.get('brands', [])
        except (OSError, ValueError):
            brands = []
    catalog: dict[str, str] = {}
    for brand in brands:
        normalized = normalize_brand_name(brand)
        if normalized:
            catalog.setdefault(normalized, brand)
    cache.set(_CACHE_KEY, catalog, timeout=None)
    return catalog


def clear_brand_catalog_cache() -> None:
    cache.delete(_CACHE_KEY)


def catalog_status() -> dict:
    """Метаданные рабочей версии для API и проверки свежести."""
    try:
        from apps.marketplaces.models import AvitoBrandCatalog
        state = AvitoBrandCatalog.objects.filter(pk=1).only('synced_at', 'brands').first()
        if state:
            return {
                'loaded': bool(state.brands),
                'synced_at': state.synced_at,
                'stale': state.synced_at < now() - CATALOG_MAX_AGE,
                'count': len(state.brands),
            }
    except Exception:
        pass
    try:
        data = json.loads(_CATALOG_PATH.read_text(encoding='utf-8'))
        from django.utils.dateparse import parse_datetime
        synced_at = parse_datetime(data.get('synced_at', ''))
        return {
            'loaded': bool(data.get('brands')),
            'synced_at': synced_at,
            'stale': not synced_at or synced_at < now() - CATALOG_MAX_AGE,
            'count': len(data.get('brands', [])),
        }
    except (OSError, ValueError, TypeError):
        return {'loaded': False, 'synced_at': None, 'stale': True, 'count': 0}


def brand_catalog_loaded() -> bool:
    """Есть ли локальный каталог брендов (без него проверка не выполняется)."""
    return bool(_catalog_by_normalized_name())


def lookup_brand(brand: str) -> dict:
    """Ищет бренд в каталоге Avito.

    Возвращает {'known': bool, 'canonical': str | None, 'suggestions': [str, ...]}.
    known=True и при отсутствии каталога (fail-open) — чтобы не заворачивать
    объявления из-за отсутствия справочника.
    """
    catalog = _catalog_by_normalized_name()
    if not catalog:
        return {'known': True, 'canonical': None, 'suggestions': []}
    normalized = normalize_brand_name(brand)
    if not normalized:
        return {'known': False, 'canonical': None, 'suggestions': []}
    canonical = catalog.get(normalized)
    if canonical is not None:
        return {'known': True, 'canonical': canonical, 'suggestions': []}
    close = difflib.get_close_matches(normalized, catalog.keys(), n=3, cutoff=0.8)
    return {
        'known': False,
        'canonical': None,
        'suggestions': [catalog[key] for key in close],
    }
