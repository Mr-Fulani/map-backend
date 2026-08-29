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
