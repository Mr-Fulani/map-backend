import json
import threading
from dataclasses import FrozenInstanceError
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
import requests
from django.test import override_settings

from apps.billing.yookassa_client import (
    PaymentSnapshot, RefundSnapshot, YooKassaAPIError, YooKassaSnapshotError,
    create_payment, fetch_payment, fetch_refund,
)


CLIENT_SETTINGS = override_settings(
    YOOKASSA_SHOP_ID='shop-id',
    YOOKASSA_SECRET_KEY='secret-key',
    YOOKASSA_API_BASE_URL='https://api.example.test/v3',
    YOOKASSA_API_CONNECT_TIMEOUT_SECONDS=1.25,
    YOOKASSA_API_READ_TIMEOUT_SECONDS=4.5,
    YOOKASSA_API_MAX_ELAPSED_SECONDS=15,
    YOOKASSA_API_MAX_RESPONSE_BYTES=1024,
)


def api_response(
    payload=None,
    status_code=200,
    *,
    raw_body=None,
    headers=None,
    chunks=None,
):
    body = raw_body if raw_body is not None else json.dumps(payload).encode('utf-8')
    response = Mock(status_code=status_code)
    response.headers = (
        {'Content-Length': str(len(body))}
        if headers is None
        else headers
    )
    response.iter_content.side_effect = lambda chunk_size: iter(
        chunks if chunks is not None else [body],
    )
    return response


@CLIENT_SETTINGS
def test_create_payment_uses_bounded_http_and_caller_idempotency_key():
    response = api_response({
        'id': 'pay_http_key',
        'status': 'pending',
        'amount': {'value': '100.00', 'currency': 'RUB'},
        'test': False,
        'confirmation': {
            'type': 'redirect',
            'confirmation_url': 'https://pay.example/confirm',
        },
    })
    with patch(
        'apps.billing.yookassa_client.requests.post',
        return_value=response,
    ) as request_post:
        result = create_payment(
            Decimal('100.00'),
            'Durable checkout',
            'https://app.example/return',
            {'checkout_intent_id': '42'},
            idempotency_key='00000000-0000-4000-8000-000000000042',
        )

    assert result == ('pay_http_key', 'https://pay.example/confirm')
    request_post.assert_called_once_with(
        'https://api.example.test/v3/payments',
        auth=('shop-id', 'secret-key'),
        headers={
            'Accept': 'application/json',
            'Accept-Encoding': 'identity',
            'Content-Type': 'application/json',
            'Idempotence-Key': '00000000-0000-4000-8000-000000000042',
        },
        json={
            'amount': {'value': '100.00', 'currency': 'RUB'},
            'confirmation': {
                'type': 'redirect',
                'return_url': 'https://app.example/return',
            },
            'capture': True,
            'description': 'Durable checkout',
            'metadata': {'checkout_intent_id': '42'},
        },
        timeout=(1.25, 4.5),
        allow_redirects=False,
        stream=True,
    )
    response.close.assert_called_once_with()


@CLIENT_SETTINGS
@pytest.mark.parametrize(
    'payload',
    [
        {
            'id': 'pay_bad_amount',
            'status': 'pending',
            'amount': {'value': '99.00', 'currency': 'RUB'},
            'test': False,
            'confirmation': {
                'type': 'redirect',
                'confirmation_url': 'https://pay.example/confirm',
            },
        },
        {
            'id': 'pay_bad_url',
            'status': 'pending',
            'amount': {'value': '100.00', 'currency': 'RUB'},
            'test': False,
            'confirmation': {
                'type': 'redirect',
                'confirmation_url': 'http://pay.example/insecure',
            },
        },
    ],
)
def test_create_payment_rejects_mismatched_or_unsafe_response(payload):
    response = api_response(payload)
    with patch(
        'apps.billing.yookassa_client.requests.post',
        return_value=response,
    ), pytest.raises(YooKassaSnapshotError):
        create_payment(
            Decimal('100.00'),
            'Durable checkout',
            'https://app.example/return',
            {'checkout_intent_id': '42'},
            idempotency_key='00000000-0000-4000-8000-000000000042',
        )
    response.close.assert_called_once_with()


@CLIENT_SETTINGS
@override_settings(YOOKASSA_API_MAX_RESPONSE_BYTES=16)
def test_create_payment_rejects_oversized_response_and_closes_connection():
    response = api_response(
        raw_body=b'{}',
        headers={'Content-Length': '17'},
    )
    with patch(
        'apps.billing.yookassa_client.requests.post',
        return_value=response,
    ), pytest.raises(YooKassaAPIError, match='лимит'):
        create_payment(
            Decimal('100.00'),
            'Durable checkout',
            'https://app.example/return',
            {'checkout_intent_id': '42'},
            idempotency_key='00000000-0000-4000-8000-000000000042',
        )

    response.iter_content.assert_not_called()
    response.close.assert_called_once_with()


@CLIENT_SETTINGS
def test_fetch_payment_uses_explicit_timeouts_and_returns_frozen_snapshot():
    response = api_response({
        'id': 'pay_123',
        'status': 'succeeded',
        'amount': {'value': '100.50', 'currency': 'rub'},
        'test': False,
        'metadata': {'tenant_id': 'attacker-controlled-and-ignored'},
    })

    with patch(
        'apps.billing.yookassa_client.requests.get',
        return_value=response,
    ) as request_get:
        snapshot = fetch_payment('pay_123')

    assert snapshot == PaymentSnapshot(
        id='pay_123',
        status='succeeded',
        amount=Decimal('100.50'),
        currency='RUB',
        test=False,
    )
    request_get.assert_called_once_with(
        'https://api.example.test/v3/payments/pay_123',
        auth=('shop-id', 'secret-key'),
        headers={
            'Accept': 'application/json',
            'Accept-Encoding': 'identity',
        },
        timeout=(1.25, 4.5),
        allow_redirects=False,
        stream=True,
    )
    response.close.assert_called_once_with()
    with pytest.raises(FrozenInstanceError):
        snapshot.status = 'canceled'


@CLIENT_SETTINGS
def test_fetch_payment_preserves_authoritative_test_flag():
    response = api_response({
        'id': 'pay_test',
        'status': 'succeeded',
        'amount': {'value': '10.00', 'currency': 'RUB'},
        'test': True,
    })

    with patch('apps.billing.yookassa_client.requests.get', return_value=response):
        snapshot = fetch_payment('pay_test')

    assert snapshot.test is True


@CLIENT_SETTINGS
def test_fetch_refund_requires_authoritative_payment_id():
    response = api_response({
        'id': 'refund_123',
        'status': 'succeeded',
        'payment_id': 'pay_123',
        'amount': {'value': '25.00', 'currency': 'RUB'},
    })
    with patch(
        'apps.billing.yookassa_client.requests.get',
        return_value=response,
    ) as request_get:
        snapshot = fetch_refund('refund_123')

    assert snapshot == RefundSnapshot(
        id='refund_123',
        status='succeeded',
        payment_id='pay_123',
        amount=Decimal('25.00'),
        currency='RUB',
    )
    assert request_get.call_args.args[0].endswith('/refunds/refund_123')


@CLIENT_SETTINGS
@pytest.mark.parametrize(
    'payload',
    [
        {
            'id': 'different_payment',
            'status': 'succeeded',
            'amount': {'value': '10.00', 'currency': 'RUB'},
        },
        {
            'id': 'pay_bad',
            'status': 'unknown-future-status',
            'amount': {'value': '10.00', 'currency': 'RUB'},
        },
        {
            'id': 'pay_bad',
            'status': 'succeeded',
            'amount': {'value': '0.00', 'currency': 'RUB'},
        },
        {
            'id': 'pay_bad',
            'status': 'succeeded',
            'amount': {'value': '1.001', 'currency': 'RUB'},
        },
        {
            'id': 'pay_bad',
            'status': 'succeeded',
            'amount': {'value': 'NaN', 'currency': 'RUB'},
        },
        {
            'id': 'pay_bad',
            'status': 'succeeded',
            'amount': {'value': '10.00', 'currency': 'RUBLE'},
        },
    ],
)
def test_fetch_payment_rejects_malformed_authoritative_snapshot(payload):
    with patch(
        'apps.billing.yookassa_client.requests.get',
        return_value=api_response(payload),
    ), pytest.raises(YooKassaSnapshotError):
        fetch_payment('pay_bad')


@CLIENT_SETTINGS
@pytest.mark.parametrize(
    'effect,response',
    [
        (requests.Timeout('timeout'), None),
        (None, api_response({}, status_code=503)),
    ],
)
def test_fetch_payment_wraps_transport_and_http_errors(effect, response):
    with patch(
        'apps.billing.yookassa_client.requests.get',
        side_effect=effect,
        return_value=response,
    ), pytest.raises(YooKassaAPIError):
        fetch_payment('pay_123')
    if response is not None:
        response.close.assert_called_once_with()


@CLIENT_SETTINGS
def test_fetch_payment_rejects_invalid_json():
    response = api_response(raw_body=b'{invalid-json')
    with patch(
        'apps.billing.yookassa_client.requests.get',
        return_value=response,
    ), pytest.raises(YooKassaAPIError):
        fetch_payment('pay_123')
    response.close.assert_called_once_with()


@CLIENT_SETTINGS
@override_settings(YOOKASSA_API_MAX_RESPONSE_BYTES=16)
def test_fetch_payment_rejects_oversized_declared_body_without_reading_it():
    response = api_response(
        raw_body=b'{}',
        headers={'Content-Length': '17'},
    )
    with patch(
        'apps.billing.yookassa_client.requests.get',
        return_value=response,
    ), pytest.raises(YooKassaAPIError, match='лимит'):
        fetch_payment('pay_oversized')

    response.iter_content.assert_not_called()
    response.close.assert_called_once_with()


@CLIENT_SETTINGS
@override_settings(YOOKASSA_API_MAX_RESPONSE_BYTES=8)
def test_fetch_payment_rejects_oversized_chunked_body_and_closes_response():
    response = api_response(
        raw_body=b'',
        headers={},
        chunks=[b'12345', b'6789'],
    )
    with patch(
        'apps.billing.yookassa_client.requests.get',
        return_value=response,
    ), pytest.raises(YooKassaAPIError, match='лимит'):
        fetch_payment('pay_chunked_oversized')

    response.close.assert_called_once_with()


@CLIENT_SETTINGS
def test_fetch_payment_rejects_compressed_body_before_decompression():
    response = api_response(
        raw_body=b'compressed',
        headers={
            'Content-Encoding': 'gzip',
            'Content-Length': '10',
        },
    )
    with patch(
        'apps.billing.yookassa_client.requests.get',
        return_value=response,
    ), pytest.raises(YooKassaAPIError, match='сжатие'):
        fetch_payment('pay_compressed')

    response.iter_content.assert_not_called()
    response.close.assert_called_once_with()


@CLIENT_SETTINGS
@override_settings(YOOKASSA_API_MAX_ELAPSED_SECONDS=0.02)
def test_fetch_payment_aborts_slow_drip_body_at_total_deadline():
    release = threading.Event()
    response = Mock(status_code=200, headers={})

    def chunks(chunk_size):
        del chunk_size
        assert release.wait(timeout=1)
        yield b'late'

    response.iter_content.side_effect = chunks
    response.close.side_effect = release.set

    with patch(
        'apps.billing.yookassa_client.requests.get',
        return_value=response,
    ), pytest.raises(YooKassaAPIError, match='лимит времени'):
        fetch_payment('pay_slow')

    assert response.close.call_count >= 1


@CLIENT_SETTINGS
def test_fetch_refund_rejects_missing_payment_id():
    response = api_response({
        'id': 'refund_123',
        'status': 'succeeded',
        'amount': {'value': '25.00', 'currency': 'RUB'},
    })
    with patch(
        'apps.billing.yookassa_client.requests.get',
        return_value=response,
    ), pytest.raises(YooKassaSnapshotError):
        fetch_refund('refund_123')
