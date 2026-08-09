"""Secret-free errors persisted for outbound tenant webhook deliveries."""

from __future__ import annotations

import requests

from apps.core.url_security import (
    RequestDeadlineExceeded,
    ResponseTooLarge,
    UnsafePublicURL,
)


class SafeWebhookDeliveryError(RuntimeError):
    """A stable operator-facing error that never contains the destination URL."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

    @property
    def persisted_message(self) -> str:
        return f'{self.code}: {self}'


def safe_webhook_delivery_error(exc: BaseException) -> SafeWebhookDeliveryError:
    """Map transport/internal failures to bounded messages without URL/query data."""
    if isinstance(exc, SafeWebhookDeliveryError):
        return exc
    if isinstance(exc, (RequestDeadlineExceeded, requests.Timeout, TimeoutError)):
        return SafeWebhookDeliveryError(
            'transport_timeout',
            'Webhook endpoint не ответил за отведённое время.',
        )
    if isinstance(exc, UnsafePublicURL):
        return SafeWebhookDeliveryError(
            'unsafe_destination',
            'Webhook endpoint больше не соответствует сетевой политике безопасности.',
        )
    if isinstance(exc, ResponseTooLarge):
        return SafeWebhookDeliveryError(
            'response_policy_error',
            'Ответ webhook endpoint нарушил ограничения транспорта.',
        )
    if isinstance(exc, requests.RequestException):
        return SafeWebhookDeliveryError(
            'transport_error',
            'Не удалось безопасно подключиться к webhook endpoint.',
        )
    return SafeWebhookDeliveryError(
        'delivery_internal_error',
        'Внутренняя ошибка подготовки или доставки webhook.',
    )
