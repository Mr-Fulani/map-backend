from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation
import json
import math
import re
import time
from urllib.parse import quote, urlsplit

import requests
from django.conf import settings

from apps.core.http_deadlines import HTTPDeadlineExceeded, enforce_response_deadline


_PROVIDER_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,200}$')
_IDEMPOTENCY_KEY_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_CURRENCY_RE = re.compile(r'^[A-Z]{3}$')
_MAX_PROVIDER_AMOUNT = Decimal('99999999.99')
_PAYMENT_STATUSES = frozenset({
    'pending',
    'waiting_for_capture',
    'succeeded',
    'canceled',
})
_REFUND_STATUSES = frozenset({'pending', 'succeeded', 'canceled'})


class YooKassaAPIError(RuntimeError):
    """YooKassa API недоступен или вернул ошибочный HTTP/JSON-ответ."""


class YooKassaSnapshotError(ValueError):
    """Ответ YooKassa не содержит полный и непротиворечивый объект."""


@dataclass(frozen=True, slots=True)
class PaymentSnapshot:
    id: str
    status: str
    amount: Decimal
    currency: str
    test: bool = False


@dataclass(frozen=True, slots=True)
class RefundSnapshot:
    id: str
    status: str
    payment_id: str
    amount: Decimal
    currency: str


def is_valid_provider_id(value: str) -> bool:
    return bool(_PROVIDER_ID_RE.fullmatch(value))


def _required_idempotency_key(value: str) -> str:
    normalized = value if isinstance(value, str) else ''
    if not _IDEMPOTENCY_KEY_RE.fullmatch(normalized):
        raise ValueError('Некорректный ключ идемпотентности YooKassa.')
    return normalized


def _required_provider_id(value, field_name: str) -> str:
    normalized = value if isinstance(value, str) else ''
    if not is_valid_provider_id(normalized):
        raise YooKassaSnapshotError(f'Некорректное поле {field_name} в ответе YooKassa.')
    return normalized


def _required_status(value, allowed_statuses: frozenset[str]) -> str:
    normalized = value if isinstance(value, str) else ''
    if normalized not in allowed_statuses:
        raise YooKassaSnapshotError('Некорректный статус объекта в ответе YooKassa.')
    return normalized


def _required_boolean(value, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise YooKassaSnapshotError(
            f'Некорректное поле {field_name} в ответе YooKassa.',
        )
    return value


def _required_amount(payload: dict) -> tuple[Decimal, str]:
    amount_payload = payload.get('amount')
    if not isinstance(amount_payload, dict):
        raise YooKassaSnapshotError('В ответе YooKassa отсутствует сумма.')
    raw_value = amount_payload.get('value')
    if isinstance(raw_value, bool):
        raise YooKassaSnapshotError('Некорректная сумма в ответе YooKassa.')
    try:
        amount = Decimal(str(raw_value))
        normalized_amount = amount.quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        raise YooKassaSnapshotError(
            'Некорректная сумма в ответе YooKassa.',
        ) from None
    if (
        not amount.is_finite()
        or amount <= 0
        or amount != normalized_amount
        or amount > _MAX_PROVIDER_AMOUNT
    ):
        raise YooKassaSnapshotError('Некорректная сумма в ответе YooKassa.')
    raw_currency = amount_payload.get('currency')
    currency = raw_currency.upper() if isinstance(raw_currency, str) else ''
    if not _CURRENCY_RE.fullmatch(currency):
        raise YooKassaSnapshotError('Некорректная валюта в ответе YooKassa.')
    return normalized_amount, currency


def _outgoing_amount(amount, currency: str) -> tuple[Decimal, str]:
    if isinstance(amount, bool):
        raise ValueError('Некорректная сумма запроса YooKassa.')
    try:
        raw_amount = Decimal(str(amount))
        normalized_amount = raw_amount.quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('Некорректная сумма запроса YooKassa.') from None
    normalized_currency = currency.upper() if isinstance(currency, str) else ''
    if (
        not raw_amount.is_finite()
        or raw_amount <= 0
        or raw_amount != normalized_amount
        or raw_amount > _MAX_PROVIDER_AMOUNT
        or not _CURRENCY_RE.fullmatch(normalized_currency)
    ):
        raise ValueError('Некорректная сумма или валюта запроса YooKassa.')
    return normalized_amount, normalized_currency


def _api_transport_settings() -> tuple[
    str,
    tuple[str, str],
    tuple[float, float],
    float,
]:
    shop_id = settings.YOOKASSA_SHOP_ID
    secret_key = settings.YOOKASSA_SECRET_KEY
    if not shop_id or not secret_key:
        raise YooKassaAPIError('Учётные данные YooKassa не настроены.')

    base_url = settings.YOOKASSA_API_BASE_URL.rstrip('/')
    parsed_base_url = urlsplit(base_url)
    if (
        parsed_base_url.scheme != 'https'
        or not parsed_base_url.hostname
        or parsed_base_url.username is not None
        or parsed_base_url.password is not None
        or parsed_base_url.query
        or parsed_base_url.fragment
    ):
        raise YooKassaAPIError('Некорректный HTTPS URL YooKassa API.')
    connect_timeout = settings.YOOKASSA_API_CONNECT_TIMEOUT_SECONDS
    read_timeout = settings.YOOKASSA_API_READ_TIMEOUT_SECONDS
    max_elapsed = settings.YOOKASSA_API_MAX_ELAPSED_SECONDS
    if (
        not isinstance(connect_timeout, (int, float))
        or isinstance(connect_timeout, bool)
        or not isinstance(read_timeout, (int, float))
        or isinstance(read_timeout, bool)
        or not math.isfinite(connect_timeout)
        or not math.isfinite(read_timeout)
        or not isinstance(max_elapsed, (int, float))
        or isinstance(max_elapsed, bool)
        or not math.isfinite(max_elapsed)
        or connect_timeout <= 0
        or read_timeout <= 0
        or max_elapsed <= 0
    ):
        raise YooKassaAPIError('Некорректные таймауты YooKassa API.')
    return (
        base_url,
        (shop_id, secret_key),
        (connect_timeout, read_timeout),
        float(max_elapsed),
    )


def _response_json(response, *, deadline: float, expected_status: int = 200) -> dict:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise YooKassaAPIError('YooKassa API превысил общий лимит времени.')
    try:
        with enforce_response_deadline(response, remaining):
            if response.status_code != expected_status:
                raise YooKassaAPIError(
                    f'YooKassa API вернул HTTP {response.status_code}.',
                )
            response_body = read_response_limited(
                response,
                settings.YOOKASSA_API_MAX_RESPONSE_BYTES,
            )
            payload = json.loads(response_body.decode('utf-8'))
    except HTTPDeadlineExceeded as exc:
        raise YooKassaAPIError(
            'YooKassa API превысил общий лимит времени.',
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise YooKassaAPIError('YooKassa API вернул невалидный JSON.') from exc
    if not isinstance(payload, dict):
        raise YooKassaSnapshotError('YooKassa API вернул объект неверного типа.')
    return payload


def read_response_limited(response, max_bytes: int) -> bytes:
    """Reads an uncompressed streamed response while enforcing the body limit."""
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise YooKassaAPIError('Некорректный лимит ответа YooKassa API.')

    content_encoding = response.headers.get('Content-Encoding')
    if (
        content_encoding is not None
        and content_encoding.strip().lower() != 'identity'
    ):
        # ``requests`` transparently decompresses response chunks. Rejecting
        # encoded bodies prevents one compressed chunk from expanding beyond
        # the application limit before ``iter_content`` can account for it.
        raise YooKassaAPIError(
            'YooKassa API вернул неподдерживаемое сжатие ответа.',
        )

    raw_length = response.headers.get('Content-Length')
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except (TypeError, ValueError):
            raise YooKassaAPIError(
                'YooKassa API вернул некорректный Content-Length.',
            ) from None
        if content_length < 0 or content_length > max_bytes:
            raise YooKassaAPIError('Ответ YooKassa API превысил лимит размера.')

    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=min(65536, max_bytes + 1)):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise YooKassaAPIError('Ответ YooKassa API превысил лимит размера.')
        chunks.append(chunk)
    return b''.join(chunks)


def _fetch_object(resource: str, object_id: str) -> dict:
    requested_id = _required_provider_id(object_id, 'id')
    base_url, auth, timeout, max_elapsed = _api_transport_settings()
    url = f'{base_url}/{resource}/{quote(requested_id, safe="")}'
    deadline = time.monotonic() + max_elapsed
    response = None
    try:
        response = requests.get(
            url,
            auth=auth,
            headers={
                'Accept': 'application/json',
                'Accept-Encoding': 'identity',
            },
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        payload = _response_json(response, deadline=deadline)
    except requests.RequestException as exc:
        raise YooKassaAPIError('Не удалось получить объект из YooKassa.') from exc
    finally:
        if response is not None:
            response.close()
    return payload


def _post_object(resource: str, payload: dict, idempotency_key: str) -> dict:
    # Refund creation intentionally remains unavailable until it has a durable
    # database intent and ambiguous-response reconciliation like checkout.
    if resource != 'payments':
        raise ValueError('Некорректный YooKassa API resource.')
    if not isinstance(payload, dict):
        raise ValueError('Payload YooKassa должен быть объектом.')
    base_url, auth, timeout, max_elapsed = _api_transport_settings()
    deadline = time.monotonic() + max_elapsed
    response = None
    try:
        response = requests.post(
            f'{base_url}/{resource}',
            auth=auth,
            headers={
                'Accept': 'application/json',
                'Accept-Encoding': 'identity',
                'Content-Type': 'application/json',
                'Idempotence-Key': _required_idempotency_key(idempotency_key),
            },
            json=payload,
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        return _response_json(response, deadline=deadline)
    except requests.RequestException as exc:
        raise YooKassaAPIError('Не удалось создать объект в YooKassa.') from exc
    finally:
        if response is not None:
            response.close()


def fetch_payment(payment_id: str) -> PaymentSnapshot:
    """Получает авторитетное состояние платежа непосредственно из YooKassa."""
    payload = _fetch_object('payments', payment_id)
    snapshot_id = _required_provider_id(payload.get('id'), 'id')
    if snapshot_id != payment_id:
        raise YooKassaSnapshotError('YooKassa вернула другой идентификатор платежа.')
    amount, currency = _required_amount(payload)
    return PaymentSnapshot(
        id=snapshot_id,
        status=_required_status(payload.get('status'), _PAYMENT_STATUSES),
        amount=amount,
        currency=currency,
        test=_required_boolean(payload.get('test'), 'test'),
    )


def fetch_refund(refund_id: str) -> RefundSnapshot:
    """Получает авторитетное состояние возврата непосредственно из YooKassa."""
    payload = _fetch_object('refunds', refund_id)
    snapshot_id = _required_provider_id(payload.get('id'), 'id')
    if snapshot_id != refund_id:
        raise YooKassaSnapshotError('YooKassa вернула другой идентификатор возврата.')
    amount, currency = _required_amount(payload)
    return RefundSnapshot(
        id=snapshot_id,
        status=_required_status(payload.get('status'), _REFUND_STATUSES),
        payment_id=_required_provider_id(payload.get('payment_id'), 'payment_id'),
        amount=amount,
        currency=currency,
    )


def create_payment(
    amount: Decimal,
    description: str,
    return_url: str,
    metadata: dict,
    *,
    idempotency_key: str,
) -> tuple[str, str]:
    """
    Создаёт платёж в YooKassa и возвращает (payment_id, confirmation_url).

    Ключ идемпотентности обязан быть создан и сохранён до сетевого
    вызова, чтобы ambiguous retry не создавал второй платёж.
    capture=True — автоматическое подтверждение без двухшагового захвата.
    """
    normalized_amount, currency = _outgoing_amount(amount, 'RUB')
    if not isinstance(description, str) or not description.strip():
        raise ValueError('Описание платежа YooKassa обязательно.')
    if len(description) > 128:
        raise ValueError('Описание платежа YooKassa слишком длинное.')
    if not isinstance(metadata, dict) or len(metadata) > 16:
        raise ValueError('Некорректные metadata платежа YooKassa.')
    for key, value in metadata.items():
        if (
            not isinstance(key, str)
            or not 1 <= len(key) <= 64
            or not isinstance(value, str)
            or len(value) > 512
        ):
            raise ValueError('Некорректные metadata платежа YooKassa.')
    try:
        parsed_return_url = urlsplit(return_url)
        parsed_return_url.port
    except (AttributeError, TypeError, ValueError):
        raise ValueError('Некорректный return_url платежа YooKassa.') from None
    if (
        parsed_return_url.scheme not in {'http', 'https'}
        or not parsed_return_url.hostname
        or parsed_return_url.username is not None
        or parsed_return_url.password is not None
    ):
        raise ValueError('Некорректный return_url платежа YooKassa.')

    payment = _post_object(
        'payments',
        {
            'amount': {'value': f'{normalized_amount:.2f}', 'currency': currency},
            'confirmation': {'type': 'redirect', 'return_url': return_url},
            'capture': True,
            'description': description.strip(),
            'metadata': metadata,
        },
        idempotency_key,
    )
    payment_id = _required_provider_id(payment.get('id'), 'id')
    _required_status(payment.get('status'), _PAYMENT_STATUSES)
    response_amount, response_currency = _required_amount(payment)
    _required_boolean(payment.get('test'), 'test')
    confirmation = payment.get('confirmation')
    if (
        not isinstance(confirmation, dict)
        or confirmation.get('type') != 'redirect'
    ):
        raise YooKassaSnapshotError('В ответе YooKassa отсутствует redirect confirmation.')
    confirmation_url = confirmation.get('confirmation_url')
    try:
        parsed_confirmation_url = urlsplit(confirmation_url)
        parsed_confirmation_url.port
    except (AttributeError, TypeError, ValueError):
        raise YooKassaSnapshotError('Некорректный confirmation URL YooKassa.') from None
    if (
        parsed_confirmation_url.scheme != 'https'
        or not parsed_confirmation_url.hostname
        or parsed_confirmation_url.username is not None
        or parsed_confirmation_url.password is not None
    ):
        raise YooKassaSnapshotError('Некорректный confirmation URL YooKassa.')
    if response_amount != normalized_amount or response_currency != currency:
        raise YooKassaSnapshotError('YooKassa вернула другую сумму платежа.')
    return payment_id, confirmation_url
