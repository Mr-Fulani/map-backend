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
from functools import lru_cache
from pathlib import Path

_CATALOG_PATH = Path(__file__).resolve().parents[2] / 'data' / 'avito_brand_catalog.json'


def normalize_brand_name(name: str) -> str:
    """Нормализует бренд для сравнения: нижний регистр, только буквы/цифры."""
    return ''.join(char for char in str(name or '').lower() if char.isalnum())


@lru_cache(maxsize=1)
def _catalog_by_normalized_name() -> dict[str, str]:
    """{нормализованное имя: каноничное написание Avito}. Пусто — файла нет."""
    try:
        data = json.loads(_CATALOG_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    catalog: dict[str, str] = {}
    for brand in data.get('brands', []):
        normalized = normalize_brand_name(brand)
        if normalized:
            catalog.setdefault(normalized, brand)
    return catalog


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
