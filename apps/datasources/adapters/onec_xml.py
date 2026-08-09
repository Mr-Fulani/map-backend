from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from lxml import etree

from apps.core.url_security import (
    REDIRECT_SAME_ORIGIN,
    request_public_http_url,
)
from apps.datasources.base import BaseDataSourceAdapter
from apps.datasources.encryption import decrypt
from apps.datasources.limits import datasource_limit
from apps.datasources.validation import validate_onec_credentials


class OneCXMLValidationError(ValueError):
    pass


_ITEM_FIELDS = frozenset({
    'UUID',
    'Article',
    'Name',
    'Brand',
    'Price',
    'StockQty',
    'Category',
    'Condition',
})


def _local_name(elem) -> str:
    try:
        return etree.QName(elem).localname
    except (TypeError, ValueError) as exc:
        raise OneCXMLValidationError('XML содержит некорректное имя элемента.') from exc


def _normalize_item(values: dict[str, str]) -> dict:
    price = values.get('Price', '')
    try:
        price_decimal = Decimal(price)
        if not price_decimal.is_finite():
            raise InvalidOperation
    except InvalidOperation:
        price_decimal = Decimal('0')

    qty = values.get('StockQty', '0')
    try:
        stock_qty = int(qty)
    except ValueError:
        stock_qty = 0

    return {
        'uuid': values.get('UUID') or None,
        'article': values.get('Article', ''),
        'name': values.get('Name', ''),
        'brand': values.get('Brand', ''),
        'price': str(price_decimal),
        'stock_qty': stock_qty,
        'category': values.get('Category', ''),
        'condition': values.get('Condition', 'new'),
    }


def _validate_pagination(limit: int, offset: int) -> None:
    max_page_items = datasource_limit('DATASOURCE_FETCH_PAGE_MAX_ITEMS')
    max_items = datasource_limit('DATASOURCE_XML_MAX_ITEMS')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= max_page_items:
        raise OneCXMLValidationError(
            f'limit должен быть целым числом от 1 до {max_page_items}.',
        )
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= max_items
    ):
        raise OneCXMLValidationError(
            f'offset должен быть целым числом от 0 до {max_items}.',
        )


def _parse_items(payload: bytes, *, offset: int, limit: int) -> list[dict]:
    """Parse a page while consuming and validating the complete XML stream."""
    _validate_pagination(limit, offset)
    if not isinstance(payload, bytes):
        raise OneCXMLValidationError('XML-выгрузка должна быть передана в байтах.')

    max_bytes = datasource_limit('DATASOURCE_XML_MAX_BYTES')
    max_nodes = datasource_limit('DATASOURCE_XML_MAX_NODES')
    max_text_chars = datasource_limit('DATASOURCE_XML_MAX_TEXT_CHARS')
    max_items = datasource_limit('DATASOURCE_XML_MAX_ITEMS')
    if len(payload) > max_bytes:
        raise OneCXMLValidationError(
            f'Размер XML-выгрузки превышает допустимый лимит {max_bytes} байт.',
        )

    result = []
    node_count = 0
    text_chars = 0
    item_count = 0
    active_item = None
    active_values: dict[str, str] | None = None

    try:
        context = etree.iterparse(
            BytesIO(payload),
            events=('start', 'end'),
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            dtd_validation=False,
            huge_tree=False,
            recover=False,
        )
        for event, elem in context:
            if event == 'start':
                node_count += 1
                if node_count > max_nodes:
                    raise OneCXMLValidationError(
                        f'Количество XML-элементов превышает допустимый лимит {max_nodes}.',
                    )
                if _local_name(elem) == 'Item':
                    if active_item is not None:
                        raise OneCXMLValidationError('Вложенные XML-элементы Item запрещены.')
                    item_count += 1
                    if item_count > max_items:
                        raise OneCXMLValidationError(
                            f'Количество XML-позиций превышает допустимый лимит {max_items}.',
                        )
                    active_item = elem
                    active_values = {}
                continue

            text_chars += len(elem.text or '') + len(elem.tail or '')
            if text_chars > max_text_chars:
                raise OneCXMLValidationError(
                    'Объём текста в XML превышает допустимый лимит '
                    f'{max_text_chars} символов.',
                )

            parent = elem.getparent()
            if (
                active_item is not None
                and elem is not active_item
                and parent is active_item
            ):
                field_name = _local_name(elem)
                if field_name in _ITEM_FIELDS and active_values is not None:
                    active_values.setdefault(field_name, (elem.text or '').strip())

            if elem is active_item:
                item_index = item_count - 1
                if offset <= item_index < offset + limit:
                    result.append(_normalize_item(active_values or {}))
                active_item = None
                active_values = None

            elem.clear()
            if parent is not None:
                while elem.getprevious() is not None:
                    del parent[0]

        root = context.root
        if root is None:
            raise OneCXMLValidationError('XML-выгрузка пуста.')
        if root.getroottree().docinfo.doctype:
            raise OneCXMLValidationError('DTD и XML entities в выгрузке не поддерживаются.')
    except OneCXMLValidationError:
        raise
    except etree.XMLSyntaxError as exc:
        raise OneCXMLValidationError(
            'XML-выгрузка повреждена или имеет небезопасный формат.',
        ) from exc

    return result


class OneCXMLAdapter(BaseDataSourceAdapter):
    def fetch_changes(
        self,
        since: datetime,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict]:
        _validate_pagination(limit, offset)
        creds = validate_onec_credentials(decrypt(self.connection.credentials))
        response = request_public_http_url(
            creds['url'],
            timeout=(5, 30),
            auth=(creds['user'], creds['password']),
            max_response_bytes=datasource_limit('DATASOURCE_XML_MAX_BYTES'),
            redirect_policy=REDIRECT_SAME_ORIGIN,
        )
        response.raise_for_status()
        return _parse_items(response.content, offset=offset, limit=limit)

    def test_connection(self) -> bool:
        self.fetch_changes(since=datetime.now(), limit=1)
        return True

    def get_display_name(self) -> str:
        return '1С XML'
