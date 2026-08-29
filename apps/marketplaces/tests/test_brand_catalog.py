"""Тесты каталога брендов Avito и проверки бренда при публикации."""
import types
from unittest.mock import patch

from apps.marketplaces.adapters.avito.brand_catalog import (
    brand_catalog_loaded, lookup_brand, normalize_brand_name,
)
from apps.marketplaces.adapters.avito.feed_builder import unknown_brand_details


def test_catalog_is_baked_in_and_large():
    assert brand_catalog_loaded()


def test_lookup_is_case_and_punctuation_insensitive():
    # «HYUNDAI / KIA» → каталожное «Hyundai-KIA» (как нормализует сам Avito).
    result = lookup_brand('HYUNDAI / KIA')
    assert result['known'] is True
    assert result['canonical'] == 'Hyundai-KIA'


def test_lookup_unknown_brand_returns_suggestions():
    result = lookup_brand('Шоффер123456')
    assert result['known'] is False
    # Явный мусор — просто не найден; а опечатка даёт подсказку:
    typo = lookup_brand('Schofer')  # пропущена буква f — в каталоге «Schoffer»
    assert typo['known'] is False
    assert 'Schoffer' in typo['suggestions']


def test_lookup_fail_open_without_catalog():
    with patch(
        'apps.marketplaces.adapters.avito.brand_catalog._catalog_by_normalized_name',
        return_value={},
    ):
        assert lookup_brand('Что угодно')['known'] is True


def test_normalize_brand_name():
    assert normalize_brand_name('HYUNDAI / KIA') == 'hyundaikia'
    assert normalize_brand_name('  NTY  ') == 'nty'
    assert normalize_brand_name('') == ''


def _listing(brand, condition='new'):
    product = types.SimpleNamespace(brand=brand, condition=condition)
    return types.SimpleNamespace(product=product)


def test_unknown_brand_details_for_new_product():
    with patch(
        'apps.marketplaces.adapters.avito.feed_builder._avito_spec',
        return_value={'required': ['Brand']},
    ):
        brand, suggestions = unknown_brand_details(_listing('НесуществующийБрендХ'))
    assert brand == 'НесуществующийБрендХ'
    assert isinstance(suggestions, list)


def test_unknown_brand_details_none_for_known_used_or_empty():
    assert unknown_brand_details(_listing('NTY')) is None
    assert unknown_brand_details(_listing('НесуществующийБрендХ', condition='used')) is None
    assert unknown_brand_details(_listing('')) is None
