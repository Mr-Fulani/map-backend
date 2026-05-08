from datetime import datetime
from decimal import Decimal, InvalidOperation

from lxml import etree

from apps.datasources.base import BaseDataSourceAdapter
from apps.datasources.encryption import decrypt


def _parse_item(elem) -> dict:
    def text(tag, default=''):
        node = elem.find(tag)
        return node.text.strip() if node is not None and node.text else default

    price = text('Price')
    try:
        price_decimal = Decimal(price)
    except InvalidOperation:
        price_decimal = Decimal('0')

    qty = text('StockQty', '0')
    try:
        stock_qty = int(qty)
    except ValueError:
        stock_qty = 0

    return {
        'uuid': text('UUID') or None,
        'article': text('Article'),
        'name': text('Name'),
        'brand': text('Brand'),
        'price': str(price_decimal),
        'stock_qty': stock_qty,
        'category': text('Category'),
        'condition': text('Condition', 'new'),
    }


class OneCXMLAdapter(BaseDataSourceAdapter):
    def fetch_changes(self, since: datetime, limit: int = 500, offset: int = 0) -> list[dict]:
        creds = decrypt(self.connection.credentials)
        import requests
        response = requests.get(
            creds['url'],
            auth=(creds.get('user', ''), creds.get('password', '')),
            timeout=30,
        )
        response.raise_for_status()
        root = etree.fromstring(response.content)
        items = root.findall('.//Item')
        result = []
        for elem in items[offset:offset + limit]:
            result.append(_parse_item(elem))
        return result

    def test_connection(self) -> bool:
        self.fetch_changes(since=__import__('datetime').datetime.now(), limit=1)
        return True

    def get_display_name(self) -> str:
        return '1С XML'
