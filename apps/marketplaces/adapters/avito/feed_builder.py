import io
import uuid
from xml.etree import ElementTree as ET

from django.conf import settings


# Соответствие значений condition из Product → Avito
_CONDITION_MAP = {
    'new': 'Новое',
    'used': 'Б/у',
    'refurbished': 'Б/у',
}

_DEFAULT_CATEGORY = 'Запчасти и аксессуары'


def build_feed(listings: list) -> bytes:
    """
    Генерирует XML-фид в формате Avito Autoload (formatVersion=3) для списка листингов.

    В фид попадают только активные объявления. Снятие с публикации в Avito
    делается ОТСУТСТВИЕМ объявления в файле (Avito архивирует то, чего нет),
    а не тегом — поэтому archived/deleted сюда передавать не нужно.
    Возвращает bytes в UTF-8 с XML-декларацией.
    """
    root = ET.Element('Ads', formatVersion='3', target='Avito.ru')

    for listing in listings:
        product = listing.product
        ad = ET.SubElement(root, 'Ad')

        # Id — наш ключ идемпотентности; по нему сопоставляем с avito_id через API
        ET.SubElement(ad, 'Id').text = str(listing.publish_idempotency_key)
        ET.SubElement(ad, 'Title').text = listing.title or product.name or ''
        ET.SubElement(ad, 'Description').text = (
            listing.description_ai or getattr(product, 'description_1c', '') or ''
        )
        ET.SubElement(ad, 'Price').text = str(int(listing.price_on_listing))
        ET.SubElement(ad, 'Category').text = _get_avito_category(listing)
        # AdType / GoodsType — обязательные параметры категории «Запчасти и аксессуары».
        # Без них Avito отклоняет объявление при обработке фида (коды 1073/1123).
        ET.SubElement(ad, 'AdType').text = (
            getattr(listing, 'ad_type', '') or 'Товар приобретен на продажу'
        )
        ET.SubElement(ad, 'GoodsType').text = _get_goods_type(listing)
        # ProductType («Тип товара») — обязателен для запчастей, но зависит от
        # конкретной детали, поэтому отдаём только если задан в маппинге категории.
        product_type = _get_product_type(listing)
        if product_type:
            ET.SubElement(ad, 'ProductType').text = product_type
        # SparePartType («Вид запчасти», напр. «Автосвет») — обязателен, но Avito
        # умеет определить его сам по названию; отдаём, если задан в маппинге.
        spare_part_type = _get_spare_part_type(listing)
        if spare_part_type:
            ET.SubElement(ad, 'SparePartType').text = spare_part_type
        # Под-вид (EngineSparePartType / BodySparePartType / …) — для категорий Avito,
        # где он обязателен (Двигатель/Кузов/Трансмиссия). Берём из дерева Avito.
        subtype_tag, subtype_value = _get_part_subtype(listing)
        if subtype_tag and subtype_value:
            ET.SubElement(ad, subtype_tag).text = subtype_value
        # Brand (Производитель) и OEM (Номер детали) — обязательны для запчастей.
        ET.SubElement(ad, 'Brand').text = _get_brand(listing)
        ET.SubElement(ad, 'OEM').text = _get_oem(listing)
        _add_placement(ad, listing)
        ET.SubElement(ad, 'Condition').text = _CONDITION_MAP.get(
            getattr(product, 'condition', ''), 'Новое'
        )
        ET.SubElement(ad, 'AllowEmail').text = 'Нет'
        _add_images(ad, product)

    ET.indent(root, space='  ')
    buf = io.BytesIO()
    ET.ElementTree(root).write(buf, encoding='UTF-8', xml_declaration=True)
    return buf.getvalue()


def get_ad_id(listing) -> str:
    """Возвращает Id объявления в фиде — используется как ключ при сопоставлении с Avito."""
    return str(listing.publish_idempotency_key)


def build_stop_feed() -> bytes:
    """
    Возвращает спец-фид «STOP» — документированная команда Avito снять ВСЕ
    объявления аккаунта с публикации (когда активных не осталось).

    Формат: <Ads target="Avito.ru" formatVersion="3"><Ad><Id>STOP</Id></Ad></Ads>
    """
    root = ET.Element('Ads', formatVersion='3', target='Avito.ru')
    ET.SubElement(ET.SubElement(root, 'Ad'), 'Id').text = 'STOP'
    ET.indent(root, space='  ')
    buf = io.BytesIO()
    ET.ElementTree(root).write(buf, encoding='UTF-8', xml_declaration=True)
    return buf.getvalue()


def _avito_spec(listing) -> dict:
    """
    Спецификация Avito (slug/fixed/required) по нашей категории товара.

    Резолвит catalog_category товара в лист Avito через category_map и берёт
    фиксированные значения полей и список обязательных полей из справочника.
    """
    from apps.marketplaces.adapters.avito.category_map import (
        avito_spec_for, leaf_spec_by_name, leaf_spec_by_slug,
    )
    category = getattr(listing.product, 'catalog_category', None)
    if not category:
        return {}
    parent = getattr(category, 'parent', None)
    # 1) Базовая таксономия — через курируемый маппинг category_map.
    spec = avito_spec_for(getattr(category, 'name', ''), getattr(parent, 'name', ''))
    if spec:
        return spec
    # 2) Категория из импортированного дерева Avito — поднимаемся к листу по slug
    # (external_id). Slug уникален, имя — нет: «Тормозная система» есть и в
    # легковой, и в грузовой ветках, поиск по имени уводил товар в грузовую.
    by_slug = leaf_spec_by_slug()
    node = category
    while node is not None:
        leaf = by_slug.get(getattr(node, 'external_id', '') or '')
        if leaf:
            return {'slug': leaf.get('slug'), 'fixed': leaf.get('fixed', {}),
                    'required': leaf.get('required', [])}
        node = getattr(node, 'parent', None)
    # 3) Легаси-фолбэк для записей без external_id — по имени (с предпочтением
    # легковой ветки при коллизии имён).
    by_name = leaf_spec_by_name()
    node = category
    while node is not None:
        leaf = by_name.get(node.name)
        if leaf:
            return {'slug': leaf.get('slug'), 'fixed': leaf.get('fixed', {}),
                    'required': leaf.get('required', [])}
        node = getattr(node, 'parent', None)
    return {}


def _avito_fixed(listing, tag: str) -> str:
    """Фиксированное значение поля Avito (tag) из маппинга категории, иначе пусто."""
    return (_avito_spec(listing).get('fixed') or {}).get(tag, '')


def _get_spare_part_type(listing) -> str:
    """«Вид запчасти» (SparePartType): attributes_map тенанта → маппинг категории → пусто."""
    mapping = _get_category_mapping(listing)
    attributes = getattr(mapping, 'attributes_map', {}) if mapping else {}
    return _first_value(
        attributes.get('SparePartType'),
        attributes.get('spare_part_type'),
        _avito_fixed(listing, 'SparePartType'),
    )


def _get_avito_category(listing) -> str:
    """Avito-категория: из спеки листа (fixed.Category) → маппинг → дефолт."""
    fixed_category = _avito_fixed(listing, 'Category')
    if fixed_category:
        return fixed_category
    mapping = _get_category_mapping(listing)
    if mapping:
        return mapping.category_target
    return _DEFAULT_CATEGORY


def _get_part_subtype(listing) -> tuple[str | None, str]:
    """
    Вид запчасти 2-го уровня (EngineSparePartType / BodySparePartType / …) и значение.

    Если у листа Avito есть под-вид (в required) и товар стоит НИЖЕ листа в дереве
    Avito, то значение под-вида = имя категории товара (напр. «Патрубки вентиляции»).
    """
    spec = _avito_spec(listing)
    sub_tag = next(
        (t for t in (spec.get('required') or []) if t.endswith('SparePartType') and t != 'SparePartType'),
        None,
    )
    if not sub_tag:
        return None, ''
    category = getattr(listing.product, 'catalog_category', None)
    if not category:
        return sub_tag, ''
    from apps.marketplaces.adapters.avito.category_map import leaf_spec_by_slug
    # Товар на самом листе Avito — вид не выбран; ниже листа — это и есть вид.
    # На листе товар стоит, если slug (external_id) или имя категории совпадают
    # с найденным листом; сравнение с конкретным листом, а не со словарём всех
    # имён — имена листьев не уникальны между ветками.
    leaf = leaf_spec_by_slug().get(spec.get('slug') or '') or {}
    external_id = getattr(category, 'external_id', '') or ''
    if external_id == spec.get('slug') or (not external_id and category.name == leaf.get('name')):
        return sub_tag, ''
    return sub_tag, category.name


def _get_goods_type(listing) -> str:
    """
    Возвращает «Вид товара» (GoodsType) для категории «Запчасти и аксессуары».

    Берёт значение из attributes_map маппинга категории, иначе — дефолт «Запчасти».
    """
    mapping = _get_category_mapping(listing)
    attributes = getattr(mapping, 'attributes_map', {}) if mapping else {}
    return _first_value(
        attributes.get('GoodsType'),
        attributes.get('goods_type'),
        _avito_fixed(listing, 'GoodsType'),
        'Запчасти',
    )


def _get_brand(listing) -> str:
    """
    Производитель (Brand) запчасти.

    Бренд товара; если пуст — fallback на название организации тенанта.
    """
    brand = str(getattr(listing.product, 'brand', '') or '').strip()
    if brand:
        return brand
    return str(getattr(getattr(listing, 'tenant', None), 'name', '') or '').strip()


def _get_oem(listing) -> str:
    """
    Номер детали OEM.

    Приоритет: oem_numbers товара → артикул → сгенерированное значение
    OEM-формата (когда нет ни OEM, ни артикула). При отсутствии настоящего
    OEM тенант предупреждается отдельно в publish_listing_task.
    """
    product = listing.product
    oem = [str(x).strip() for x in (getattr(product, 'oem_numbers', None) or []) if str(x).strip()]
    if oem:
        return ', '.join(oem)
    article = str(getattr(product, 'article', '') or '').strip()
    if article:
        return article
    return f'NA{uuid.uuid4().hex[:10].upper()}'


def product_has_oem(listing) -> bool:
    """True, если у товара есть настоящие OEM-номера (а не подставленный артикул)."""
    return bool([x for x in (getattr(listing.product, 'oem_numbers', None) or []) if str(x).strip()])


def _get_product_type(listing) -> str:
    """
    Возвращает «Тип товара» (ProductType): attributes_map тенанта → маппинг категории.

    Для запчастей это класс техники («Для автомобилей»), для шин/масел — конкретный
    тип («Легковые шины», «Моторные масла»). Если не задано — тег в фид не попадёт.
    """
    mapping = _get_category_mapping(listing)
    attributes = getattr(mapping, 'attributes_map', {}) if mapping else {}
    return _first_value(
        attributes.get('ProductType'),
        attributes.get('product_type'),
        _avito_fixed(listing, 'ProductType'),
    )


# Поля, которые build_feed формирует всегда. CompatibleCars Avito определяет сам
# по названию/совместимости (объявления публикуются без него) — не считаем его дырой.
_FEED_PROVIDED_TAGS = {
    'Id', 'Title', 'Description', 'Price', 'Category', 'GoodsType', 'ProductType',
    'SparePartType', 'AdType', 'Brand', 'OEM', 'Condition', 'Address', 'SellerAddressID',
    'Images', 'CompatibleCars',
}


def missing_required_avito_fields(listing) -> list[str]:
    """
    Обязательные поля листа Avito, которые фид не заполняет (по справочнику категории).

    Используется для предупреждения тенанта: для таких товаров (напр. шины без
    размеров, масла без вязкости) Avito может отклонить объявление. Возвращает
    список тегов; пусто — если категория не сопоставлена или всё заполняется.
    """
    required = _avito_spec(listing).get('required') or []
    provided = set(_FEED_PROVIDED_TAGS)
    subtype_tag, subtype_value = _get_part_subtype(listing)
    if subtype_tag and subtype_value:
        provided.add(subtype_tag)  # под-вид заполнен из дерева — не считаем дырой
    return [tag for tag in required if tag not in provided]


# Под-виды запчастей: обязательные поля, без которых Avito гарантированно
# отклоняет объявление («Не заполнен обязательный параметр …», code 1000).
# В карточке листинга это «Подкатегория 3» — вид детали ниже листа Avito.
AVITO_SUBTYPE_LABELS = {
    'TransmissionSparePartType': 'Тип детали трансмиссии',
    'EngineSparePartType': 'Тип детали двигателя',
    'BodySparePartType': 'Тип детали кузова',
    'TechnicSparePartType': 'Тип детали',
}


def blocking_missing_avito_fields(listing) -> list[str]:
    """Недостающие поля, из-за которых Avito гарантированно отклонит объявление.

    В отличие от остального required-списка (CompatibleCars и т.п. Avito умеет
    определять сам), под-вид детали реально блокирует публикацию — проверено
    отчётами автозагрузки.
    """
    return [tag for tag in missing_required_avito_fields(listing) if tag in AVITO_SUBTYPE_LABELS]


def avito_field_warnings(listing) -> list[str]:
    """Человекочитаемые предупреждения о незаполненных обязательных полях Avito.

    Показываются тенанту в карточке листинга ДО публикации, чтобы не ловить
    отклонение Avito постфактум.
    """
    category = getattr(listing.product, 'catalog_category', None)
    category_name = getattr(category, 'name', '') or 'категории товара'
    warnings = []
    for tag in missing_required_avito_fields(listing):
        label = AVITO_SUBTYPE_LABELS.get(tag)
        if label:
            warnings.append(
                f'Не выбран вид детали — «{label}» ({tag}). Для категории '
                f'«{category_name}» Avito не опубликует объявление без него: '
                f'выберите «Подкатегорию 3» в карточке листинга или уточните категорию товара.'
            )
        else:
            warnings.append(
                f'Не заполнено обязательное поле Avito «{tag}» — объявление может быть '
                f'отклонено. Заполните его у товара или в маппинге категории.'
            )
    return warnings


def get_contact_fields(listing) -> tuple[str, str]:
    """
    Возвращает (контактное лицо, телефон) по тем же приоритетам, что и фид.

    Используется фидом и валидацией публикации, чтобы не отправлять в Avito
    объявления с пустыми контактами.
    """
    mapping = _get_category_mapping(listing)
    attributes = getattr(mapping, 'attributes_map', {}) if mapping else {}
    account = listing.account
    manual_address = getattr(listing, 'placement_address', None)
    bulk_address = getattr(listing, 'bulk_placement_address', None)
    account_address = _get_account_default_address(account)

    manager_name = _first_value(
        getattr(manual_address, 'manager_name', ''),
        getattr(listing, 'manager_name_override', ''),
        getattr(bulk_address, 'manager_name', ''),
        getattr(listing, 'bulk_manager_name', ''),
        attributes.get('manager_name'),
        attributes.get('ManagerName'),
        getattr(account_address, 'manager_name', ''),
        getattr(account, 'default_manager_name', ''),
    )
    contact_phone = _first_value(
        getattr(manual_address, 'contact_phone', ''),
        getattr(listing, 'contact_phone_override', ''),
        getattr(bulk_address, 'contact_phone', ''),
        getattr(listing, 'bulk_contact_phone', ''),
        attributes.get('contact_phone'),
        attributes.get('ContactPhone'),
        getattr(account_address, 'contact_phone', ''),
        getattr(account, 'default_contact_phone', ''),
    )
    return manager_name, contact_phone


def _get_category_mapping(listing):
    try:
        from apps.marketplaces.models import CategoryMapping
        qs = CategoryMapping.objects.filter(
            tenant=listing.tenant,
            marketplace=CategoryMapping.MARKETPLACE_AVITO,
        )
        # Приоритет — категория из источника (1С). Если по ней маппинга нет,
        # пробуем по имени категории каталога: импортированные из дерева Avito
        # маппинги ключуются именно по имени листа (см. AvitoCatalogImporter).
        candidates = []
        if listing.product.category_1c:
            candidates.append(listing.product.category_1c)
        catalog_category = getattr(listing.product, 'catalog_category', None)
        if catalog_category is not None:
            candidates.append(catalog_category.name)
        for source in candidates:
            mapping = qs.filter(category_source=source).first()
            if mapping:
                return mapping
        return None
    except Exception:
        return None


def has_resolved_category(listing) -> bool:
    """
    True, если у листинга определена категория для Avito.

    Категория считается определённой, если у товара задана catalog_category
    (из неё берётся спецификация полей) либо нашёлся CategoryMapping. Иначе фид
    уйдёт с дефолтной Avito-категорией и, скорее всего, будет отклонён.
    """
    if getattr(listing.product, 'catalog_category', None) is not None:
        return True
    return _get_category_mapping(listing) is not None


def _add_placement(ad, listing) -> None:
    """Добавляет адрес и контактные поля из листинга, категории или аккаунта."""
    mapping = _get_category_mapping(listing)
    attributes = getattr(mapping, 'attributes_map', {}) if mapping else {}
    account = listing.account
    manual_address = getattr(listing, 'placement_address', None)
    bulk_address = getattr(listing, 'bulk_placement_address', None)
    account_address = _get_account_default_address(account)

    seller_address_id = _first_value(
        getattr(manual_address, 'seller_address_id', ''),
        getattr(listing, 'seller_address_id_override', ''),
        getattr(bulk_address, 'seller_address_id', ''),
        getattr(listing, 'bulk_seller_address_id', ''),
        attributes.get('seller_address_id'),
        attributes.get('SellerAddressID'),
        getattr(account_address, 'seller_address_id', ''),
        getattr(account, 'default_seller_address_id', ''),
    )
    address = _first_value(
        getattr(manual_address, 'address', ''),
        getattr(listing, 'address_override', ''),
        getattr(bulk_address, 'address', ''),
        getattr(listing, 'bulk_address', ''),
        attributes.get('address'),
        attributes.get('Address'),
        getattr(account_address, 'address', ''),
        getattr(account, 'default_address', ''),
    )
    manager_name, contact_phone = get_contact_fields(listing)

    # Защита от мусорного значения: external_id аккаунта — это не ID адреса.
    # Avito такой SellerAddressID не находит, поэтому игнорируем и шлём текстовый адрес.
    if seller_address_id and seller_address_id == str(getattr(account, 'external_id', '') or ''):
        seller_address_id = ''

    if seller_address_id:
        ET.SubElement(ad, 'SellerAddressID').text = seller_address_id
    elif address:
        ET.SubElement(ad, 'Address').text = address
    if manager_name:
        ET.SubElement(ad, 'ManagerName').text = manager_name
    if contact_phone:
        ET.SubElement(ad, 'ContactPhone').text = contact_phone


def _get_account_default_address(account):
    if not account:
        return None
    try:
        return account.placement_addresses.filter(is_active=True, is_default=True).first()
    except Exception:
        return None


def _first_value(*values) -> str:
    for value in values:
        text = str(value or '').strip()
        if text:
            return text
    return ''


def _add_images(ad, product) -> None:
    """Добавляет в фид только одобренные/ручные/импортированные фото."""
    from apps.products.media import get_publishable_product_images

    urls = []
    for image in get_publishable_product_images(product):
        url = _image_url(image.s3_key, image.url_source)
        if url.startswith('http'):
            urls.append(url)

    if not urls:
        category = getattr(product, 'catalog_category', None)
        if category and category.default_image_s3_key:
            url = _image_url(category.default_image_s3_key, '')
            if url.startswith('http'):
                urls.append(url)

    if not urls:
        return

    images = ET.SubElement(ad, 'Images')
    for url in urls[:10]:
        ET.SubElement(images, 'Image', url=url)


def _image_url(s3_key: str, fallback: str) -> str:
    """Строит публичный URL изображения для фида."""
    from django.core.files.storage import default_storage

    cdn = getattr(settings, 'YC_CDN_DOMAIN', '')
    is_s3 = hasattr(default_storage, 'bucket_name')
    if cdn and s3_key and is_s3:
        return f'https://{cdn}/{s3_key}'
    if s3_key:
        return default_storage.url(s3_key)
    return fallback or ''
