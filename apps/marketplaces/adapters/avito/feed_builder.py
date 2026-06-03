import io
from xml.etree import ElementTree as ET


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
        ET.SubElement(ad, 'Condition').text = _CONDITION_MAP.get(
            getattr(product, 'condition', ''), 'Новое'
        )
        ET.SubElement(ad, 'AllowEmail').text = 'Нет'

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
    try:
        from apps.marketplaces.models import CategoryMapping
        mapping = CategoryMapping.objects.filter(
            tenant=listing.tenant,
            category_source=listing.product.category_1c,
        ).first()
        if mapping:
            return mapping.category_target
    except Exception:
        pass
    return _DEFAULT_CATEGORY
