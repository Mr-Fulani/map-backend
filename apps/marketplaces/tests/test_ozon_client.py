import json

import pytest
import requests
from django.test import override_settings

from apps.marketplaces.adapters.ozon.client import (
    OzonAPIError,
    OzonSellerClient,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict, headers: dict | None = None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.content = json.dumps(payload).encode()
        self.encoding = 'utf-8'
        self.closed = False

    def iter_content(self, chunk_size: int, decode_unicode: bool = False):
        del chunk_size, decode_unicode
        return iter([self.content])

    def json(self):
        return self._payload

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses: list[FakeResponse | Exception]):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@override_settings(
    OZON_API_RESPONSE_MAX_BYTES=100_000,
    OZON_API_MAX_PAGES=3,
    OZON_API_TIMEOUT_SECONDS=2,
)
def test_verify_connection_uses_current_read_only_endpoints_and_normalizes_data():
    roles_response = FakeResponse(
        200, {
            'roles': [
                {
                    'name': 'Product API',
                    'methods': [
                        '/v3/product/list',
                        {'name': '/v1/roles'},
                    ],
                },
                'Finance API',
            ],
            'expires_at': '2026-09-03T09:00:00Z',
        },
    )
    seller_response = FakeResponse(200, {
            'company': {'name': 'АльфаПро', 'currency': 'RUB'},
            'seller': {'name': 'Alfa Seller'},
    })
    warehouse_response = FakeResponse(200, {
            'warehouses': [{'warehouse_id': 42, 'name': 'Основной склад'}],
            'has_next': False,
            'cursor': '',
    })
    session = FakeSession([
        roles_response,
        seller_response,
        warehouse_response,
    ])

    snapshot = OzonSellerClient(
        client_id='12345',
        api_key='top-secret',
        session=session,
    ).verify_connection()

    assert [call[0] for call in session.calls] == [
        'https://api-seller.ozon.ru/v1/roles',
        'https://api-seller.ozon.ru/v1/seller/info',
        'https://api-seller.ozon.ru/v2/warehouse/list',
    ]
    assert session.calls[2][1]['json'] == {'limit': 100}
    assert session.calls[0][1]['headers']['Client-Id'] == '12345'
    assert session.calls[0][1]['headers']['Api-Key'] == 'top-secret'
    assert session.calls[0][1]['allow_redirects'] is False
    assert snapshot.company_name == 'АльфаПро'
    assert snapshot.seller_name == 'Alfa Seller'
    assert snapshot.currency == 'RUB'
    assert snapshot.roles == ('Finance API', 'Product API')
    assert snapshot.api_methods == ('/v1/roles', '/v3/product/list')
    assert snapshot.api_key_expires_at.isoformat() == '2026-09-03T09:00:00+00:00'
    assert snapshot.warehouses[0].warehouse_id == '42'
    assert roles_response.closed is True
    assert seller_response.closed is True
    assert warehouse_response.closed is True


@override_settings(
    OZON_API_RESPONSE_MAX_BYTES=100_000,
    OZON_API_MAX_PAGES=3,
    OZON_API_TIMEOUT_SECONDS=2,
)
def test_warehouse_list_follows_bounded_cursor_pagination():
    first = FakeResponse(200, {
        'warehouses': [{'warehouse_id': '1', 'name': 'Первый'}],
        'has_next': True,
        'cursor': 'next-page',
    })
    second = FakeResponse(200, {
        'warehouses': [{'warehouse_id': '2', 'name': 'Второй'}],
        'has_next': False,
        'cursor': '',
    })
    session = FakeSession([first, second])

    warehouses = OzonSellerClient(
        client_id='cid', api_key='key', session=session,
    ).list_warehouses()

    assert [warehouse.warehouse_id for warehouse in warehouses] == ['1', '2']
    assert session.calls[1][1]['json'] == {'limit': 100, 'cursor': 'next-page'}


@override_settings(
    OZON_API_RESPONSE_MAX_BYTES=100_000,
    OZON_CATALOG_RESPONSE_MAX_BYTES=250_000,
    OZON_API_MAX_PAGES=3,
    OZON_API_TIMEOUT_SECONDS=2,
)
def test_catalog_methods_use_only_read_only_description_category_endpoints():
    tree_response = FakeResponse(200, {
        'result': [{
            'description_category_id': 10,
            'category_name': 'Автотовары',
            'disabled': False,
            'children': [],
        }],
    })
    attribute_response = FakeResponse(200, {
        'result': [{
            'id': 85,
            'name': 'Бренд',
            'type': 'String',
        }],
    })
    value_response = FakeResponse(200, {
        'result': [{'id': 501, 'value': 'Test Brand'}],
    })
    session = FakeSession([tree_response, attribute_response, value_response])
    client = OzonSellerClient(
        client_id='5741594',
        api_key='read-only-key',
        session=session,
    )

    tree = client.get_description_category_tree(language='RU')
    attributes = client.get_description_category_attributes(
        description_category_id=10,
        type_id=20,
        language='RU',
    )
    values = client.search_description_category_attribute_values(
        description_category_id=10,
        type_id=20,
        attribute_id=85,
        value='Test',
    )

    assert tree[0]['description_category_id'] == 10
    assert attributes[0]['id'] == 85
    assert values[0]['id'] == 501
    assert [call[0] for call in session.calls] == [
        'https://api-seller.ozon.ru/v1/description-category/tree',
        'https://api-seller.ozon.ru/v1/description-category/attribute',
        'https://api-seller.ozon.ru/v1/description-category/attribute/values/search',
    ]
    assert session.calls[0][1]['json'] == {'language': 'RU'}
    assert session.calls[1][1]['json'] == {
        'description_category_id': 10,
        'type_id': 20,
        'language': 'RU',
    }
    assert session.calls[2][1]['json'] == {
        'description_category_id': 10,
        'type_id': 20,
        'attribute_id': 85,
        'value': 'Test',
        'limit': 100,
    }


@override_settings(OZON_API_RESPONSE_MAX_BYTES=100_000, OZON_API_TIMEOUT_SECONDS=2)
def test_commerce_methods_use_exact_price_and_stock_contracts():
    session = FakeSession([
        FakeResponse(200, {'result': [{'offer_id': 'map-1', 'updated': True}]}),
        FakeResponse(200, {'result': [{'offer_id': 'map-1', 'warehouse_id': 42, 'updated': True}]}),
    ])
    client = OzonSellerClient(client_id='cid', api_key='key', session=session)
    price = {'offer_id': 'map-1', 'product_id': 7, 'price': '1000.00'}
    stock = {'offer_id': 'map-1', 'product_id': 7, 'warehouse_id': 42, 'stock': 2}

    assert client.update_prices([price])[0]['updated'] is True
    assert client.update_stocks([stock])[0]['updated'] is True
    assert session.calls[0][0].endswith('/v1/product/import/prices')
    assert session.calls[0][1]['json'] == {'prices': [price]}
    assert session.calls[1][0].endswith('/v2/products/stocks')
    assert session.calls[1][1]['json'] == {'stocks': [stock]}


@override_settings(OZON_API_RESPONSE_MAX_BYTES=100_000, OZON_API_TIMEOUT_SECONDS=2)
def test_fbs_order_list_uses_bounded_read_contract():
    session = FakeSession([FakeResponse(200, {'result': {
        'postings': [{'posting_number': '1'}], 'has_next': False,
    }})])
    client = OzonSellerClient(client_id='cid', api_key='key', session=session)
    postings, has_next = client.list_fbs_postings(
        since='2026-09-01T00:00:00+00:00', to='2026-09-02T00:00:00+00:00',
    )
    assert postings == [{'posting_number': '1'}]
    assert has_next is False
    assert session.calls[0][0].endswith('/v3/posting/fbs/list')
    assert session.calls[0][1]['json']['limit'] == 100
    assert session.calls[0][1]['json']['with'] == {
        'analytics_data': False, 'barcodes': False, 'financial_data': False,
    }


@override_settings(
    OZON_API_RESPONSE_MAX_BYTES=100_000,
    OZON_CATALOG_RESPONSE_MAX_BYTES=250_000,
    OZON_API_MAX_PAGES=3,
    OZON_API_TIMEOUT_SECONDS=2,
)
def test_catalog_methods_reject_non_list_results():
    session = FakeSession([
        FakeResponse(200, {'result': {'unexpected': 'object'}}),
    ])

    with pytest.raises(OzonAPIError, match='дерево категорий') as raised:
        OzonSellerClient(
            client_id='cid',
            api_key='key',
            session=session,
        ).get_description_category_tree()

    assert raised.value.code == 'invalid_response'


@override_settings(
    OZON_API_RESPONSE_MAX_BYTES=100_000,
    OZON_API_MAX_PAGES=3,
    OZON_API_TIMEOUT_SECONDS=2,
)
def test_product_import_uses_current_endpoint_and_returns_task_id():
    session = FakeSession([FakeResponse(200, {'result': {'task_id': 731}})])
    item = {'offer_id': 'map-offer-1', 'name': 'Тестовый товар'}

    task_id = OzonSellerClient(
        client_id='cid', api_key='secret', session=session,
    ).import_products([item])

    assert task_id == '731'
    assert session.calls[0][0] == 'https://api-seller.ozon.ru/v3/product/import'
    assert session.calls[0][1]['json'] == {'items': [item]}


def test_product_import_rejects_empty_or_oversized_local_batches():
    client = OzonSellerClient(client_id='cid', api_key='secret', session=FakeSession([]))

    with pytest.raises(ValueError, match='1 to 100'):
        client.import_products([])
    with pytest.raises(ValueError, match='1 to 100'):
        client.import_products([{'offer_id': str(index)} for index in range(101)])


@override_settings(
    OZON_API_RESPONSE_MAX_BYTES=100_000,
    OZON_API_MAX_PAGES=3,
    OZON_API_TIMEOUT_SECONDS=2,
)
def test_product_reconciliation_reads_task_and_exact_offer_projection():
    session = FakeSession([
        FakeResponse(200, {'result': {
            'items': [{'offer_id': 'map-1', 'status': 'imported', 'errors': []}],
        }}),
        FakeResponse(200, {'items': [{
            'id': 77,
            'offer_id': 'map-1',
            'statuses': {'moderate_status': 'approved'},
        }]}),
    ])
    client = OzonSellerClient(client_id='cid', api_key='secret', session=session)

    task = client.get_product_import_info('task-1')
    product = client.get_product_info_by_offer_id('map-1')

    assert task['items'][0]['status'] == 'imported'
    assert product and product['id'] == 77
    assert [call[0] for call in session.calls] == [
        'https://api-seller.ozon.ru/v1/product/import/info',
        'https://api-seller.ozon.ru/v3/product/info/list',
    ]
    assert session.calls[0][1]['json'] == {'task_id': 'task-1'}
    assert session.calls[1][1]['json'] == {
        'offer_id': ['map-1'],
        'product_id': [],
        'sku': [],
    }


@override_settings(
    OZON_API_RESPONSE_MAX_BYTES=100_000,
    OZON_API_MAX_PAGES=3,
    OZON_API_TIMEOUT_SECONDS=2,
)
def test_product_projection_requires_one_exact_offer_match():
    missing = OzonSellerClient(
        client_id='cid',
        api_key='secret',
        session=FakeSession([FakeResponse(200, {'items': []})]),
    )
    duplicate = OzonSellerClient(
        client_id='cid',
        api_key='secret',
        session=FakeSession([FakeResponse(200, {'items': [
            {'offer_id': 'map-1'},
            {'offer_id': 'map-1'},
        ]})]),
    )

    assert missing.get_product_info_by_offer_id('map-1') is None
    with pytest.raises(OzonAPIError, match='несколько товаров'):
        duplicate.get_product_info_by_offer_id('map-1')


@override_settings(
    OZON_API_RESPONSE_MAX_BYTES=100_000,
    OZON_API_MAX_PAGES=3,
    OZON_API_TIMEOUT_SECONDS=2,
)
def test_rate_limit_is_normalized_without_secret_or_response_body():
    session = FakeSession([
        FakeResponse(
            429,
            {'message': 'provider body must stay private'},
            {'Retry-After': '17'},
        ),
    ])

    with pytest.raises(OzonAPIError) as raised:
        OzonSellerClient(
            client_id='cid', api_key='super-secret', session=session,
        ).get_roles()

    assert raised.value.code == 'rate_limited'
    assert raised.value.retry_after_seconds == 17
    assert 'super-secret' not in str(raised.value)
    assert 'provider body' not in str(raised.value)


@override_settings(
    OZON_API_RESPONSE_MAX_BYTES=100_000,
    OZON_API_MAX_PAGES=3,
    OZON_API_TIMEOUT_SECONDS=2,
)
def test_repeated_warehouse_cursor_fails_closed():
    session = FakeSession([
        FakeResponse(200, {
            'warehouses': [],
            'has_next': True,
            'cursor': 'same',
        }),
        FakeResponse(200, {
            'warehouses': [],
            'has_next': True,
            'cursor': 'same',
        }),
    ])

    with pytest.raises(OzonAPIError, match='курсор') as raised:
        OzonSellerClient(
            client_id='cid', api_key='key', session=session,
        ).list_warehouses()

    assert raised.value.code == 'invalid_response'


@override_settings(
    OZON_API_RESPONSE_MAX_BYTES=100_000,
    OZON_API_MAX_PAGES=3,
    OZON_API_TIMEOUT_SECONDS=2,
)
def test_invalid_key_expiry_fails_closed():
    session = FakeSession([
        FakeResponse(200, {'roles': [], 'expires_at': 'not-a-date'}),
    ])

    with pytest.raises(OzonAPIError, match='срок действия') as raised:
        OzonSellerClient(
            client_id='cid', api_key='key', session=session,
        ).get_roles()

    assert raised.value.code == 'invalid_response'


@pytest.mark.parametrize(
    ('status_code', 'expected_code'),
    [
        (401, 'invalid_credentials'),
        (403, 'invalid_credentials'),
        (500, 'provider_unavailable'),
        (503, 'provider_unavailable'),
    ],
)
@override_settings(
    OZON_API_RESPONSE_MAX_BYTES=100_000,
    OZON_API_MAX_PAGES=3,
    OZON_API_TIMEOUT_SECONDS=2,
)
def test_provider_http_failures_are_normalized(status_code, expected_code):
    session = FakeSession([
        FakeResponse(status_code, {'secret_provider_detail': 'not exposed'}),
    ])

    with pytest.raises(OzonAPIError) as raised:
        OzonSellerClient(
            client_id='cid', api_key='key', session=session,
        ).get_roles()

    assert raised.value.code == expected_code
    assert 'secret_provider_detail' not in str(raised.value)


@override_settings(
    OZON_API_RESPONSE_MAX_BYTES=100_000,
    OZON_API_MAX_PAGES=3,
    OZON_API_TIMEOUT_SECONDS=2,
)
def test_provider_timeout_is_normalized():
    session = FakeSession([requests.Timeout('socket timeout')])

    with pytest.raises(OzonAPIError) as raised:
        OzonSellerClient(
            client_id='cid', api_key='key', session=session,
        ).get_roles()

    assert raised.value.code == 'connection_error'
    assert str(raised.value) == 'Не удалось связаться с Ozon Seller API.'
