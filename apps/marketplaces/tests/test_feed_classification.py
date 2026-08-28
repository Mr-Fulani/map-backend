import types
from unittest.mock import patch

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


def test_blocking_missing_fields_for_leaf_requiring_subtype():
    # Товар «на самом листе» «Трансмиссия и привод» без под-вида → блокирующее
    # поле TransmissionSparePartType и человекочитаемое предупреждение.
    from apps.marketplaces.adapters.avito.feed_builder import (
        avito_field_warnings, blocking_missing_avito_fields,
    )
    transmission = _cat('Трансмиссия и привод', external_id='transmissiia_i_privod')
    listing = _listing(transmission)

    assert blocking_missing_avito_fields(listing) == ['TransmissionSparePartType']
    warnings = avito_field_warnings(listing)
    assert any(
        'тип детали трансмиссии' in w
        and 'Категория Avito' in w
        and 'Подкатегорию 3' not in w
        for w in warnings
    )


def test_no_blocking_fields_when_subtype_selected():
    # Под-вид выбран (товар ниже листа) → блокирующих полей нет.
    from apps.marketplaces.adapters.avito.feed_builder import blocking_missing_avito_fields
    transmission = _cat('Трансмиссия и привод', external_id='transmissiia_i_privod')
    mount = _cat('Крепёж КПП', parent=transmission)
    listing = _listing(mount)

    assert blocking_missing_avito_fields(listing) == []


def test_brand_warning_for_new_product_without_brand():
    # Новая запчасть без производителя → предупреждение (Avito валидирует Brand
    # по своему каталогу, фолбэк на имя тенанта отклоняется «Значение не найдено»).
    from apps.marketplaces.adapters.avito.feed_builder import (
        avito_field_warnings, product_brand_is_missing,
    )
    category = _cat('Подвеска', external_id='podveska')
    product = types.SimpleNamespace(
        catalog_category=category, category_1c='', brand='', condition='new',
    )
    listing = types.SimpleNamespace(product=product)

    assert product_brand_is_missing(listing) is True
    assert any('производитель' in w.lower() for w in avito_field_warnings(listing))


def test_publication_errors_are_grouped_by_editable_drawer_field():
    from apps.marketplaces.adapters.avito.feed_builder import avito_publication_field_errors

    category = _cat('Подвеска', external_id='podveska')
    product = types.SimpleNamespace(
        catalog_category=category,
        category_1c='',
        name='Деталь',
        description_1c='',
        brand='',
        condition='new',
    )
    account = types.SimpleNamespace(
        is_active=True,
        external_id='account-1',
        default_manager_name='',
        default_contact_phone='',
        placement_addresses=types.SimpleNamespace(filter=lambda **kwargs: types.SimpleNamespace(first=lambda: None)),
    )
    listing = types.SimpleNamespace(
        product=product,
        account=account,
        title='Деталь',
        description_ai='',
        price_on_listing=0,
        placement_address=None,
        bulk_placement_address=None,
        manager_name_override='',
        contact_phone_override='',
        bulk_manager_name='',
        bulk_contact_phone='',
    )

    errors = avito_publication_field_errors(listing)

    assert set(errors) >= {
        'description_ai',
        'price_on_listing',
        'manager_name_override',
        'contact_phone_override',
        'product_brand',
    }


def test_no_brand_warning_for_used_product():
    # Для б/у запчастей Brand у Avito не обязателен — не предупреждаем.
    from apps.marketplaces.adapters.avito.feed_builder import product_brand_is_missing
    product = types.SimpleNamespace(
        catalog_category=None, category_1c='', brand='', condition='used',
    )
    listing = types.SimpleNamespace(product=product)

    assert product_brand_is_missing(listing) is False


def test_battery_warning_uses_plain_russian_names_and_groups_fields():
    from apps.marketplaces.adapters.avito.feed_builder import avito_field_warnings

    category = _cat('Аккумуляторы', external_id='akkumuliatory_5530')
    warnings = avito_field_warnings(_listing(category))

    assert len(warnings) == 1
    warning = warnings[0]
    assert 'напряжение аккумулятора (например, 12 В)' in warning
    assert 'ёмкость аккумулятора (например, 60 А·ч)' in warning
    assert 'пусковой ток аккумулятора (например, 540 А)' in warning
    assert 'полярность аккумулятора (прямая или обратная)' in warning
    assert 'длина детали (в миллиметрах)' in warning
    assert 'ширина детали (в миллиметрах)' in warning
    assert 'высота детали (в миллиметрах)' in warning
    assert 'поддержку MAP' in warning
    for technical_tag in (
        'Voltage', 'Capacity', 'DCL', 'Polarity',
        'TechnicLength', 'TechnicWidth', 'TechnicHeight',
    ):
        assert technical_tag not in warning


def test_unknown_brand_suggestion_warns_not_to_replace_brand_blindly():
    from apps.marketplaces.adapters.avito.feed_builder import avito_field_warnings

    category = _cat('Аккумуляторы', external_id='akkumuliatory_5530')
    product = types.SimpleNamespace(
        catalog_category=category,
        category_1c='',
        brand='AKOM',
        condition='new',
    )
    listing = types.SimpleNamespace(product=product)

    with patch(
        'apps.marketplaces.adapters.avito.brand_catalog.lookup_brand',
        return_value={'known': False, 'suggestions': ['TAKOMA']},
    ):
        warnings = avito_field_warnings(listing)

    brand_warning = warnings[0]
    assert 'Avito не распознал производителя «AKOM»' in brand_warning
    assert '«TAKOMA»' in brand_warning
    assert 'только в том случае, если это действительно тот же производитель' in brand_warning


def test_every_current_avito_required_field_has_a_user_friendly_name():
    from apps.marketplaces.adapters.avito.category_map import leaf_spec_by_slug
    from apps.marketplaces.adapters.avito.feed_builder import (
        AVITO_FIELD_LABELS, AVITO_SUBTYPE_LABELS, _FEED_PROVIDED_TAGS,
    )

    required_tags = {
        tag
        for spec in leaf_spec_by_slug().values()
        for tag in (spec.get('required') or [])
    }
    warning_tags = required_tags - _FEED_PROVIDED_TAGS - set(AVITO_SUBTYPE_LABELS)

    assert warning_tags <= set(AVITO_FIELD_LABELS)


def test_name_fallback_prefers_passenger_branch_on_collision():
    # Легаси-запись без external_id: имя «Тормозная система» есть в легковой
    # и грузовой ветках — фолбэк по имени должен выбрать легковую.
    from apps.marketplaces.adapters.avito.category_map import leaf_spec_by_name

    leaf = leaf_spec_by_name()['Тормозная система']
    assert leaf['slug'] == 'tormoznaia_sistema_5539'
    assert 'Для грузовиков и спецтехники' not in leaf.get('path', [])
