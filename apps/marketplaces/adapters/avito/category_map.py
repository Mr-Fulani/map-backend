"""Маппинг внутренней таксономии запчастей на листья категорий Avito.

Каждая наша категория сопоставляется со slug листового узла Avito. Фиксированные
значения полей (GoodsType / ProductType / SparePartType) и список обязательных
полей берутся из справочника data/avito_field_specs.json по этому slug —
дублировать их здесь не нужно (источник истины — Avito user-docs API,
обновляется командой sync_avito_categories).
"""
import gzip
import json
from functools import lru_cache
from pathlib import Path

_SPECS_PATH = Path(__file__).resolve().parents[2] / 'data' / 'avito_field_specs.json'
_RULES_PATH = _SPECS_PATH.with_name('avito_field_rules.json.gz')


@lru_cache(maxsize=1)
def _field_rules() -> dict:
    if not _RULES_PATH.exists():
        return {}
    return json.loads(gzip.decompress(_RULES_PATH.read_bytes()))


@lru_cache(maxsize=1)
def _specs() -> dict:
    """Загружает справочник листьев Avito: {slug: {name, path, required, fixed}}."""
    data = json.loads(_SPECS_PATH.read_text(encoding='utf-8'))
    rules = _field_rules()
    return {
        leaf['slug']: {**leaf, 'field_rules': rules.get(leaf['slug'], {})}
        for leaf in data['leaves']
    }


@lru_cache(maxsize=1)
def leaf_spec_by_slug() -> dict:
    """Справочник листьев Avito по slug: {slug: {name, path, fixed, required}}.

    Основной способ резолва для категорий из импортированного дерева Avito —
    у них external_id хранит slug узла, а slug в отличие от имени уникален.
    """
    return _specs()


# Ветки Avito, которые проигрывают легковой при коллизии имён листьев
# («Тормозная система» есть и у легковых, и у грузовиков — предпочитаем легковую).
_SECONDARY_BRANCHES = (
    'Для грузовиков и спецтехники',
    'Для мототехники',
    'Для водного транспорта',
)


def _is_secondary_branch(leaf: dict) -> bool:
    """Лежит ли лист в неосновной (грузовой/мото/водной) ветке дерева Avito."""
    return any(branch in (leaf.get('path') or []) for branch in _SECONDARY_BRANCHES)


@lru_cache(maxsize=1)
def leaf_spec_by_name() -> dict:
    """Справочник листьев Avito по имени: {name: {slug, fixed, required}}.

    Легаси-фолбэк для категорий дерева Avito без external_id (slug). Имена
    листьев не уникальны (например «Тормозная система» есть в легковой и
    грузовой ветках) — при коллизии предпочитаем лист легковой ветки.
    """
    by_name: dict = {}
    for leaf in _specs().values():
        current = by_name.get(leaf['name'])
        if current is None or (_is_secondary_branch(current) and not _is_secondary_branch(leaf)):
            by_name[leaf['name']] = leaf
    return by_name


# Корень нашей таксономии → slug листа Avito (значение по умолчанию для всех его листьев).
ROOT_TO_AVITO = {
    'Тормозная система': 'tormoznaia_sistema_5539',
    'Подвеска и рулевое управление': 'podveska',
    'Двигатель': 'dvigatel',
    'Фильтры': 'dvigatel',
    'Ремни и приводные элементы': 'dvigatel',
    'Топливная система': 'toplivnaia_i_vyxlopnaia_sistemy',
    'Охлаждение, отопление и кондиционер': 'sistema_oxlazdeniia',
    'Электрика и зажигание': 'elektrooborudovanie',
    'Трансмиссия и сцепление': 'transmissiia_i_privod',
    'Кузов, стекла и оптика': 'kuzov',
    'Выхлопная система': 'toplivnaia_i_vyxlopnaia_sistemy',
    'Стеклоочистители и омыватель': 'elektrooborudovanie',
    'Колеса и шины': 'kolesa',
    'Масла, жидкости и автохимия': 'drugie_masla',
    'Крепеж, инструмент и аксессуары': 'drugoe_parts',
}

# Наш лист таксономии → slug листа Avito. Переопределяет ROOT_TO_AVITO там, где
# конкретный лист уходит в другую категорию Avito, чем его корень.
LEAF_TO_AVITO = {
    # Подвеска и рулевое → у Avito рулевое отдельно
    'Рулевые тяги и наконечники': 'rulevoe_upravlenie',
    'Рулевые рейки': 'rulevoe_upravlenie',
    # Электрика → отдельные листья Avito
    'Аккумуляторы': 'akkumuliatory_5530',
    'Лампы автомобильные': 'avtosvet',
    # Кузов/оптика → стёкла и свет у Avito отдельно
    'Стекла': 'stekla',
    'Фары': 'avtosvet',
    'Фонари': 'avtosvet',
    # Колёса и шины → листья ветки «Шины, диски и колёса»
    'Шины': 'legkovye_shiny',
    'Диски колесные': 'diski',
    'Крепеж колес': 'dlia_koles',
    # Масла и жидкости → листья ветки «Масла и автохимия»
    'Моторные масла': 'motornye_masla',
    'Трансмиссионные масла': 'transmissionnye_masla',
    'Тормозные жидкости': 'tormoznye_zidkosti',
    'Антифризы': 'oxlazdaiushhie_zidkosti',
    'Смазки и очистители': 'promyvocnye_zidkosti_prisadki_i_smazki',
    # Инструмент → ветка «Инструменты»
    'Инструмент': 'ruchnoi_instrument_2304150',
}


def resolve_avito_slug(category_name: str, parent_name: str = '') -> str | None:
    """Возвращает slug листа Avito для нашей категории: лист → корень → корень родителя."""
    name = (category_name or '').strip()
    parent = (parent_name or '').strip()
    if name in LEAF_TO_AVITO:
        return LEAF_TO_AVITO[name]
    if name in ROOT_TO_AVITO:
        return ROOT_TO_AVITO[name]
    if parent in ROOT_TO_AVITO:
        return ROOT_TO_AVITO[parent]
    return None


def avito_spec_for(category_name: str, parent_name: str = '') -> dict:
    """
    Возвращает спецификацию Avito для нашей категории.

    {'slug', 'fixed': {tag: value}, 'required': [tag, ...]} либо пустой dict,
    если категория не сопоставлена. fixed/required берутся из справочника.
    """
    slug = resolve_avito_slug(category_name, parent_name)
    if not slug:
        return {}
    spec = _specs().get(slug)
    if not spec:
        return {}
    return {
        'slug': slug,
        'fixed': spec.get('fixed', {}),
        'required': spec.get('required', []),
        'field_rules': spec.get('field_rules', {}),
    }
