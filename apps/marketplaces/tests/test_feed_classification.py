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


def _preflight_listing(category, *, brand='Bosch'):
    product = types.SimpleNamespace(
        catalog_category=category,
        category_1c='',
        name='Деталь',
        description_1c='Описание',
        brand=brand,
        condition='new',
        oem_numbers=[],
    )
    account = types.SimpleNamespace(
        is_active=True,
        deleted_at=None,
        external_id='account-1',
        default_address='Москва, Тверская улица, 1',
        default_seller_address_id='',
        default_manager_name='Менеджер',
        default_contact_phone='+79990000000',
        placement_addresses=types.SimpleNamespace(
            filter=lambda **kwargs: types.SimpleNamespace(first=lambda: None),
        ),
    )
    return types.SimpleNamespace(
        product=product,
        account=account,
        title='Деталь',
        description_ai='Описание',
        price_on_listing=1000,
        placement_address=None,
        bulk_placement_address=None,
        address_override='',
        seller_address_id_override='',
        bulk_address='',
        bulk_seller_address_id='',
        manager_name_override='',
        contact_phone_override='',
        bulk_manager_name='',
        bulk_contact_phone='',
    )


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
    # поле TransmissionSparePartType и человекочитаемая красная ошибка.
    from apps.marketplaces.adapters.avito.feed_builder import (
        avito_publication_field_errors, blocking_missing_avito_fields,
    )
    transmission = _cat('Трансмиссия и привод', external_id='transmissiia_i_privod')
    listing = _preflight_listing(transmission)

    assert blocking_missing_avito_fields(listing) == ['TransmissionSparePartType']
    with patch(
        'apps.marketplaces.adapters.avito.feed_builder.get_feed_image_urls',
        return_value=([], False),
    ):
        errors = avito_publication_field_errors(listing)
    assert any(
        'Тип детали трансмиссии' in message
        for message in errors['catalog_category']
    )


def test_no_blocking_fields_when_subtype_selected():
    # Под-вид выбран (товар ниже листа) → блокирующих полей нет.
    from apps.marketplaces.adapters.avito.feed_builder import blocking_missing_avito_fields
    transmission = _cat('Трансмиссия и привод', external_id='transmissiia_i_privod')
    mount = _cat('Крепёж КПП', parent=transmission)
    listing = _listing(mount)

    assert blocking_missing_avito_fields(listing) == []


def test_brand_is_conditionally_required_for_new_avtosvet():
    from apps.marketplaces.adapters.avito.feed_builder import (
        product_brand_is_missing,
    )
    category = _cat('Автосвет', external_id='avtosvet')
    product = types.SimpleNamespace(
        catalog_category=category, category_1c='', brand='', condition='new',
    )
    listing = types.SimpleNamespace(product=product)

    assert product_brand_is_missing(listing) is True
    product.condition = 'used'
    assert product_brand_is_missing(listing) is False


def test_required_brand_is_a_blocker_for_battery_category():
    from apps.marketplaces.adapters.avito.feed_builder import (
        avito_publication_field_errors,
    )
    listing = _preflight_listing(
        _cat('Аккумуляторы', external_id='akkumuliatory_5530'),
        brand='',
    )

    with patch(
        'apps.marketplaces.adapters.avito.feed_builder.get_feed_image_urls',
        return_value=([], False),
    ):
        errors = avito_publication_field_errors(listing)

    assert 'product_brand' in errors
    assert 'обязателен' in errors['product_brand'][0]


def test_unknown_optional_brand_is_yellow_and_does_not_block():
    from apps.marketplaces.adapters.avito.feed_builder import (
        avito_publication_preflight,
    )
    listing = _preflight_listing(
        _cat('Несуществующая категория'),
        brand='НесуществующийБрендХYZ',
    )

    with (
        patch(
            'apps.marketplaces.adapters.avito.brand_catalog.lookup_brand',
            return_value={
                'known': False,
                'canonical': None,
                'suggestions': ['Bosch'],
            },
        ),
        patch(
            'apps.marketplaces.adapters.avito.feed_builder.get_feed_image_urls',
            return_value=([], False),
        ),
    ):
        errors, warnings = avito_publication_preflight(listing)

    assert 'product_brand' not in errors
    assert 'product_brand' in warnings
    assert 'не добавит его в XML' in warnings['product_brand'][0]


def test_multiple_oems_send_one_value_and_show_yellow_explanation():
    from apps.marketplaces.adapters.avito.feed_builder import (
        avito_publication_preflight,
    )
    listing = _preflight_listing(_cat('Автосвет', external_id='avtosvet'))
    listing.product.oem_numbers = ['92402D5000', '92402D4000']

    with patch(
        'apps.marketplaces.adapters.avito.feed_builder.get_feed_image_urls',
        return_value=([], False),
    ):
        errors, warnings = avito_publication_preflight(listing)

    assert 'product_oem' not in errors
    assert 'product_oem' in warnings
    assert 'несколько OEM-номеров' in warnings['product_oem'][0]
    assert 'MAP отправит «92402D5000»' in warnings['product_oem'][0]


def test_missing_oem_is_red_when_avito_condition_makes_it_required():
    from apps.marketplaces.adapters.avito.feed_builder import (
        avito_publication_preflight,
    )
    listing = _preflight_listing(_cat('Автосвет', external_id='avtosvet'))

    with patch(
        'apps.marketplaces.adapters.avito.feed_builder.get_feed_image_urls',
        return_value=([], False),
    ):
        errors, _warnings = avito_publication_preflight(listing)

    assert 'product_oem' in errors
    assert 'нового товара' in errors['product_oem'][0]


def test_one_valid_optional_oem_has_no_warning():
    from apps.marketplaces.adapters.avito.feed_builder import (
        avito_publication_preflight,
    )
    listing = _preflight_listing(_cat('Автосвет', external_id='avtosvet'))
    listing.product.oem_numbers = ['92402D4000']

    with patch(
        'apps.marketplaces.adapters.avito.feed_builder.get_feed_image_urls',
        return_value=([], False),
    ):
        _errors, warnings = avito_publication_preflight(listing)

    assert 'product_oem' not in warnings


def test_dependency_evaluator_supports_value_empty_filled_and_visibility():
    from apps.marketplaces.adapters.avito.feed_builder import _required_avito_fields

    listing = _preflight_listing(_cat('Несуществующая категория'))
    spec = {
        'required': ['VisibleOnly'],
        'fixed': {},
        'field_rules': {
            'OEM': [{
                'required': False,
                'dependencies': [{
                    'action': 'required', 'clause': 'and',
                    'pairs': [{'tag': 'Condition', 'clause': 'value', 'values': ['Новое']}],
                }],
            }],
            'NeedsBrand': [{
                'required': False,
                'dependencies': [{
                    'action': 'required', 'clause': 'and',
                    'pairs': [{'tag': 'Brand', 'clause': 'filled', 'values': []}],
                }],
            }],
            'VisibleOnly': [{
                'required': True,
                'dependencies': [{
                    'action': 'visible', 'clause': 'and',
                    'pairs': [{'tag': 'OEM', 'clause': 'empty', 'values': []}],
                }],
            }],
        },
    }

    required = _required_avito_fields(listing, spec=spec)
    assert required == ['VisibleOnly', 'OEM', 'NeedsBrand']
    listing.product.oem_numbers = ['92402D4000']
    assert _required_avito_fields(listing, spec=spec) == ['OEM', 'NeedsBrand']


def test_sync_normalizes_current_avito_dependency_shape():
    from apps.marketplaces.management.commands.sync_avito_categories import (
        _dependency_rules,
    )

    rules = _dependency_rules({
        'dependencies': [{
            'action': 'required',
            'clause': 'and',
            'pairs': [{
                'source_field_tag': 'Condition',
                'clause': 'value',
                'values': ['Новое'],
            }],
        }],
    })

    assert rules == [{
        'action': 'required',
        'clause': 'and',
        'pairs': [{'tag': 'Condition', 'clause': 'value', 'values': ['Новое']}],
    }]


def test_publication_errors_are_grouped_by_editable_drawer_field():
    from apps.marketplaces.adapters.avito.feed_builder import avito_publication_field_errors

    category = _cat('Аккумуляторы', external_id='akkumuliatory_5530')
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
        address_override='',
        seller_address_id_override='',
        bulk_address='',
        bulk_seller_address_id='',
    )

    with patch(
        'apps.marketplaces.adapters.avito.feed_builder.get_feed_image_urls',
        return_value=([], False),
    ):
        errors = avito_publication_field_errors(listing)

    assert set(errors) >= {
        'description_ai',
        'price_on_listing',
        'manager_name_override',
        'contact_phone_override',
        'product_brand',
        'placement_address',
    }


def test_no_brand_warning_for_used_product():
    # Condition не превращает optional Brand категории «Автосвет» в required.
    from apps.marketplaces.adapters.avito.feed_builder import product_brand_is_missing
    product = types.SimpleNamespace(
        catalog_category=_cat('Автосвет', external_id='avtosvet'),
        category_1c='', brand='', condition='used',
    )
    listing = types.SimpleNamespace(product=product)

    assert product_brand_is_missing(listing) is False


def test_battery_required_error_uses_plain_russian_names_and_groups_fields():
    from apps.marketplaces.adapters.avito.feed_builder import (
        avito_publication_field_errors,
    )

    category = _cat('Аккумуляторы', external_id='akkumuliatory_5530')
    listing = _preflight_listing(category)
    with patch(
        'apps.marketplaces.adapters.avito.feed_builder.get_feed_image_urls',
        return_value=([], False),
    ):
        errors = avito_publication_field_errors(listing)

    message = ' '.join(errors['catalog_category'])
    assert 'напряжение аккумулятора (например, 12 В)' in message
    assert 'ёмкость аккумулятора (например, 60 А·ч)' in message
    assert 'пусковой ток аккумулятора (например, 540 А)' in message
    assert 'полярность аккумулятора (прямая или обратная)' in message
    assert 'длина детали (в миллиметрах)' in message
    assert 'ширина детали (в миллиметрах)' in message
    assert 'высота детали (в миллиметрах)' in message
    for technical_tag in (
        'Voltage', 'Capacity', 'DCL', 'Polarity',
        'TechnicLength', 'TechnicWidth', 'TechnicHeight',
    ):
        assert technical_tag not in message


def test_unknown_brand_suggestion_warns_not_to_replace_brand_blindly():
    from apps.marketplaces.adapters.avito.feed_builder import (
        avito_publication_field_errors,
    )

    category = _cat('Аккумуляторы', external_id='akkumuliatory_5530')
    listing = _preflight_listing(category, brand='AKOM')

    with patch(
        'apps.marketplaces.adapters.avito.brand_catalog.lookup_brand',
        return_value={'known': False, 'suggestions': ['TAKOMA']},
    ):
        with patch(
            'apps.marketplaces.adapters.avito.feed_builder.get_feed_image_urls',
            return_value=([], False),
        ):
            errors = avito_publication_field_errors(listing)

    brand_warning = errors['product_brand'][0]
    assert 'Avito не распознал производителя «AKOM»' in brand_warning
    assert '«TAKOMA»' in brand_warning
    assert 'только в том случае, если это действительно тот же производитель' in brand_warning


def test_every_current_avito_required_field_has_a_user_friendly_name():
    from apps.marketplaces.adapters.avito.category_map import leaf_spec_by_slug
    from apps.marketplaces.adapters.avito.feed_builder import (
        AVITO_FIELD_LABELS, AVITO_SUBTYPE_LABELS,
        _FEED_ALWAYS_OR_PROVIDER_INFERRED_TAGS,
    )

    required_tags = {
        tag
        for spec in leaf_spec_by_slug().values()
        for tag in (spec.get('required') or [])
    }
    warning_tags = (
        required_tags
        - _FEED_ALWAYS_OR_PROVIDER_INFERRED_TAGS
        - set(AVITO_SUBTYPE_LABELS)
        - {'Brand'}
    )

    assert warning_tags <= set(AVITO_FIELD_LABELS)


def test_name_fallback_prefers_passenger_branch_on_collision():
    # Легаси-запись без external_id: имя «Тормозная система» есть в легковой
    # и грузовой ветках — фолбэк по имени должен выбрать легковую.
    from apps.marketplaces.adapters.avito.category_map import leaf_spec_by_name

    leaf = leaf_spec_by_name()['Тормозная система']
    assert leaf['slug'] == 'tormoznaia_sistema_5539'
    assert 'Для грузовиков и спецтехники' not in leaf.get('path', [])
