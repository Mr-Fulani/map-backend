import types

from apps.marketplaces.adapters.avito.feed_builder import (
    _avito_spec, _get_avito_category, _get_part_subtype,
)


def _listing(category):
    product = types.SimpleNamespace(catalog_category=category, category_1c='')
    return types.SimpleNamespace(product=product)


def _cat(name, parent=None, external_id=''):
    return types.SimpleNamespace(name=name, parent=parent, external_id=external_id)


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


def test_slug_resolves_before_name_on_collision():
    # «Сцепление» в легковой ветке — вид ниже листа «Трансмиссия и привод»,
    # а в грузовой — самостоятельный лист с тем же именем. Резолв по slug
    # (external_id) родителя должен дать легковую спеку, а не грузовую.
    transmission = _cat('Трансмиссия и привод', external_id='transmissiia_i_privod')
    clutch = _cat('Сцепление', parent=transmission)
    listing = _listing(clutch)

    spec = _avito_spec(listing)
    assert spec.get('slug') == 'transmissiia_i_privod'
    assert spec.get('fixed', {}).get('ProductType') == 'Для автомобилей'

    tag, value = _get_part_subtype(listing)
    assert tag == 'TransmissionSparePartType'
    assert value == 'Сцепление'


def test_truck_leaf_resolves_by_own_slug():
    # Грузовое «Сцепление» с собственным slug уходит в грузовую спеку.
    transmission = _cat('Трансмиссия', external_id='transmissiia')
    clutch = _cat('Сцепление', parent=transmission, external_id='sceplenie')
    listing = _listing(clutch)

    spec = _avito_spec(listing)
    assert spec.get('slug') == 'sceplenie'
    assert spec.get('fixed', {}).get('ProductType') == 'Для грузовиков и спецтехники'


def test_name_fallback_prefers_passenger_branch_on_collision():
    # Легаси-запись без external_id: имя «Тормозная система» есть в легковой
    # и грузовой ветках — фолбэк по имени должен выбрать легковую.
    from apps.marketplaces.adapters.avito.category_map import leaf_spec_by_name

    leaf = leaf_spec_by_name()['Тормозная система']
    assert leaf['slug'] == 'tormoznaia_sistema_5539'
    assert 'Для грузовиков и спецтехники' not in leaf.get('path', [])
