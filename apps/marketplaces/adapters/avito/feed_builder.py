import io
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

    Листинги со статусом ARCHIVED или DELETED включаются с тегом <Status>Remove</Status>.
    Возвращает bytes в UTF-8 с XML-декларацией.
    """
    from apps.marketplaces.models import Listing

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
        _add_placement(ad, listing)
        ET.SubElement(ad, 'Condition').text = _CONDITION_MAP.get(
            getattr(product, 'condition', ''), 'Новое'
        )
        ET.SubElement(ad, 'AllowEmail').text = 'Нет'
        _add_images(ad, product)

        if listing.status in (Listing.STATUS_ARCHIVED, Listing.STATUS_DELETED):
            ET.SubElement(ad, 'Status').text = 'Remove'

    ET.indent(root, space='  ')
    buf = io.BytesIO()
    ET.ElementTree(root).write(buf, encoding='UTF-8', xml_declaration=True)
    return buf.getvalue()


def get_ad_id(listing) -> str:
    """Возвращает Id объявления в фиде — используется как ключ при сопоставлении с Avito."""
    return str(listing.publish_idempotency_key)


def _get_avito_category(listing) -> str:
    """Ищет Avito-категорию через CategoryMapping; при отсутствии — дефолт."""
    mapping = _get_category_mapping(listing)
    if mapping:
        return mapping.category_target
    return _DEFAULT_CATEGORY


def _get_category_mapping(listing):
    try:
        from apps.marketplaces.models import CategoryMapping
        return CategoryMapping.objects.filter(
            tenant=listing.tenant,
            marketplace=CategoryMapping.MARKETPLACE_AVITO,
            category_source=listing.product.category_1c,
        ).first()
    except Exception:
        return None


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
