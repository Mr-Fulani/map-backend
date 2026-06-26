import types

from apps.marketplaces.adapters.avito.feed_builder import (
    _avito_spec, _get_avito_category, _get_part_subtype,
)


def _listing(category):
    product = types.SimpleNamespace(catalog_category=category, category_1c='')
    return types.SimpleNamespace(product=product)


def _cat(name, parent=None):
    return types.SimpleNamespace(name=name, parent=parent)


def test_avito_leaf_resolves_spare_part_type():
    # Товар на листе Avito «Двигатель» → SparePartType=Двигатель из спеки.
    listing = _listing(_cat('Двигатель'))
    spec = _avito_spec(listing)
    assert spec.get('fixed', {}).get('SparePartType') == 'Двигатель'
    assert _get_avito_category(listing) == 'Запчасти и аксессуары'


def test_avito_subtype_value_from_tree_node():
    # Товар на виде «Патрубки вентиляции» (ниже листа «Двигатель») →
    # SparePartType=Двигатель + EngineSparePartType=Патрубки вентиляции.
    engine = _cat('Двигатель')
    vent = _cat('Патрубки вентиляции', parent=engine)
    listing = _listing(vent)

    spec = _avito_spec(listing)
    assert spec.get('fixed', {}).get('SparePartType') == 'Двигатель'

    tag, value = _get_part_subtype(listing)
    assert tag == 'EngineSparePartType'
    assert value == 'Патрубки вентиляции'


def test_leaf_without_subtype_has_no_subtype():
    # «Подвеска» — лист без под-вида в required.
    listing = _listing(_cat('Подвеска'))
    tag, _value = _get_part_subtype(listing)
    assert tag is None


def test_unmapped_category_returns_empty_spec():
    listing = _listing(_cat('Несуществующая категория'))
    assert _avito_spec(listing) == {}
