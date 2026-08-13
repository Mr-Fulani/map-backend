from datetime import timedelta
import hashlib
import json
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.notifications.email import EmailNotifier
from apps.notifications.models import (
    NotificationDelivery,
    TenantNotificationSettings,
)
from apps.notifications.telegram import TelegramNotifier

LEVEL_ERROR = 'error'
LEVEL_CRITICAL = 'critical'
LEVEL_BILLING = 'billing'
LEVEL_SUCCESS = 'success'
_LEVELS = frozenset({LEVEL_ERROR, LEVEL_CRITICAL, LEVEL_BILLING, LEVEL_SUCCESS})


class NotificationDeliveryError(RuntimeError):
    """One or more configured notification channels failed to deliver."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class NotificationDeliveryInProgress(NotificationDeliveryError):
    """Another worker still owns the same channel delivery."""


def _fingerprint_channel_payload(*, recipient: str, subject: str, body: str) -> str:
    encoded = json.dumps(
        {'recipient': recipient, 'subject': subject, 'body': body},
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _normalized_event_key(event_key: str | None) -> str:
    normalized = str(event_key or f'direct:{uuid.uuid4()}').strip()
    if not normalized or len(normalized) > 200 or any(
        ord(character) < 32 for character in normalized
    ):
        raise ValueError('Некорректный ключ события уведомления.')
    return normalized


def _email_retry_deadline(delivery: NotificationDelivery):
    # Resend retains provider idempotency keys for 24 hours. Keep one hour of
    # safety margin so an automatic retry can never become a second send.
    return delivery.created_at + timedelta(hours=23)


@transaction.atomic
def _claim_channel(
    *,
    tenant,
    event_key: str,
    channel: str,
    payload_fingerprint: str,
) -> tuple[NotificationDelivery | None, NotificationDeliveryError | None]:
    type(tenant).objects.select_for_update().only('pk').get(pk=tenant.pk)
    delivery, _ = NotificationDelivery.objects.get_or_create(
        tenant=tenant,
        event_key=event_key,
        channel=channel,
        defaults={'payload_fingerprint': payload_fingerprint},
    )
    delivery = NotificationDelivery.objects.select_for_update().get(pk=delivery.pk)
    if delivery.payload_fingerprint != payload_fingerprint:
        raise ValueError('Ключ события уведомления повторно использован с другим payload.')
    if delivery.status in {
        NotificationDelivery.Status.SENT,
        NotificationDelivery.Status.SKIPPED,
    }:
        return None, None
    if delivery.status == NotificationDelivery.Status.FAILED:
        return None, NotificationDeliveryError(
            'Канал уведомления завершился постоянной ошибкой.', retryable=False,
        )

    current = timezone.now()
    stale_before = current - timedelta(
        seconds=settings.NOTIFICATION_DELIVERY_CLAIM_TIMEOUT_SECONDS,
    )
    if delivery.status == NotificationDelivery.Status.SENDING:
        if delivery.claimed_at is not None and delivery.claimed_at > stale_before:
            return None, NotificationDeliveryInProgress(
                'Канал уведомления уже отправляется.', retryable=True,
            )
        if channel == NotificationDelivery.Channel.TELEGRAM:
            # Telegram sendMessage has no provider idempotency key. A worker
            # loss after the request crossed the boundary cannot be replayed.
            delivery.status = NotificationDelivery.Status.OUTCOME_UNCERTAIN
            delivery.error_code = 'telegram_worker_lost'
            delivery.finished_at = current
            delivery.save(update_fields=[
                'status', 'error_code', 'finished_at', 'updated_at',
            ])

    if delivery.status == NotificationDelivery.Status.OUTCOME_UNCERTAIN:
        if (
            channel != NotificationDelivery.Channel.EMAIL
            or current >= _email_retry_deadline(delivery)
        ):
            return None, NotificationDeliveryError(
                'Результат отправки канала требует ручной сверки.', retryable=False,
            )

    if (
        channel == NotificationDelivery.Channel.EMAIL
        and current >= _email_retry_deadline(delivery)
    ):
        delivery.status = NotificationDelivery.Status.OUTCOME_UNCERTAIN
        delivery.error_code = 'email_idempotency_window_expired'
        delivery.finished_at = current
        delivery.save(update_fields=[
            'status', 'error_code', 'finished_at', 'updated_at',
        ])
        return None, NotificationDeliveryError(
            'Окно безопасного повтора email истекло.', retryable=False,
        )

    delivery.status = NotificationDelivery.Status.SENDING
    delivery.claimed_at = current
    delivery.attempts += 1
    delivery.error_code = ''
    delivery.save(update_fields=[
        'status', 'claimed_at', 'attempts', 'error_code', 'updated_at',
    ])
    return delivery, None


@transaction.atomic
def _finish_channel(
    delivery_id,
    *,
    status: str,
    error_code: str = '',
) -> None:
    delivery = NotificationDelivery.objects.select_for_update().get(pk=delivery_id)
    if delivery.status != NotificationDelivery.Status.SENDING:
        return
    delivery.status = status
    delivery.error_code = error_code[:80]
    delivery.finished_at = (
        timezone.now()
        if status != NotificationDelivery.Status.PENDING
        else None
    )
    delivery.save(update_fields=[
        'status', 'error_code', 'finished_at', 'updated_at',
    ])


@transaction.atomic
def mark_notification_event_exhausted(*, tenant_id: int, event_key: str) -> int:
    """Make an exhausted retry sequence visible and operator-reconcilable."""
    from apps.tenants.models import Tenant

    Tenant.objects.select_for_update().only('pk').get(pk=tenant_id)
    current = timezone.now()
    return NotificationDelivery.objects.filter(
        tenant_id=tenant_id,
        event_key=event_key,
        status=NotificationDelivery.Status.PENDING,
    ).update(
        status=NotificationDelivery.Status.OUTCOME_UNCERTAIN,
        error_code='automatic_retries_exhausted',
        finished_at=current,
        updated_at=current,
    )


class NotificationService:
    """
    Маршрутизирует уведомления тенанта по каналам в зависимости от уровня.

    Уровни:
    - error    → Telegram (если notify_on_error=True)
    - success  → Telegram (если Telegram привязан)
    - critical → Telegram @mention + Email (если notify_on_critical=True)
    - billing  → только Email

    Если настройки уведомлений для тенанта отсутствуют — молча выходит.
    """

    def notify(
        self,
        tenant,
        level: str,
        message: str,
        payload: dict | None = None,
        *,
        event_key: str | None = None,
    ) -> None:
        """
        Отправляет уведомление тенанту через соответствующие каналы.

        payload сохраняется для логирования, но не передаётся в сами сообщения.
        """
        if level not in _LEVELS:
            raise ValueError('Неизвестный уровень уведомления.')
        normalized_event_key = _normalized_event_key(event_key)
        try:
            ns = tenant.notification_settings
        except TenantNotificationSettings.DoesNotExist:
            return

        failed_channels: list[str] = []
        retryable_failure = False
        if level == LEVEL_ERROR and ns.notify_on_error:
            failure = self._deliver_telegram(
                tenant,
                normalized_event_key,
                ns.telegram_chat_id,
                f'⚠️ {message}',
            )
            if failure is not None:
                failed_channels.append('telegram')
                retryable_failure = retryable_failure or failure.retryable

        elif level == LEVEL_SUCCESS:
            failure = self._deliver_telegram(
                tenant,
                normalized_event_key,
                ns.telegram_chat_id,
                f'✅ {message}',
            )
            if failure is not None:
                failed_channels.append('telegram')
                retryable_failure = retryable_failure or failure.retryable

        elif level == LEVEL_CRITICAL and ns.notify_on_critical:
            telegram_failure = self._deliver_telegram(
                tenant,
                normalized_event_key,
                ns.telegram_chat_id,
                f'🚨 КРИТИЧНО: {message}',
            )
            email_failure = self._deliver_email(
                tenant,
                normalized_event_key,
                ns.notify_email,
                'Критическая ошибка MAP',
                message,
            )
            for channel, failure in (
                ('telegram', telegram_failure),
                ('email', email_failure),
            ):
                if failure is not None:
                    failed_channels.append(channel)
                    retryable_failure = retryable_failure or failure.retryable

        elif level == LEVEL_BILLING:
            failure = self._deliver_email(
                tenant,
                normalized_event_key,
                ns.notify_email,
                'Уведомление о биллинге MAP',
                message,
            )
            if failure is not None:
                failed_channels.append('email')
                retryable_failure = retryable_failure or failure.retryable

        if failed_channels:
            raise NotificationDeliveryError(
                'Не доставлены каналы: ' + ', '.join(failed_channels),
                retryable=retryable_failure,
            )

    def _deliver_telegram(
        self,
        tenant,
        event_key: str,
        chat_id: str,
        message: str,
    ) -> NotificationDeliveryError | None:
        """Deliver once; ambiguous Telegram outcomes are never auto-replayed."""
        if not chat_id:
            return None
        fingerprint = _fingerprint_channel_payload(
            recipient=chat_id,
            subject='',
            body=message,
        )
        delivery, claim_error = _claim_channel(
            tenant=tenant,
            event_key=event_key,
            channel=NotificationDelivery.Channel.TELEGRAM,
            payload_fingerprint=fingerprint,
        )
        if claim_error is not None:
            return claim_error
        if delivery is None:
            return None
        try:
            sent = TelegramNotifier().send(chat_id, message)
        except Exception:
            # A process-local error before the notifier returns still cannot
            # prove whether Telegram accepted the request.
            _finish_channel(
                delivery.pk,
                status=NotificationDelivery.Status.OUTCOME_UNCERTAIN,
                error_code='telegram_outcome_uncertain',
            )
            return NotificationDeliveryError(
                'Результат Telegram sendMessage неизвестен.',
                retryable=False,
            )
        if sent:
            _finish_channel(delivery.pk, status=NotificationDelivery.Status.SENT)
            return None
        _finish_channel(
            delivery.pk,
            status=NotificationDelivery.Status.OUTCOME_UNCERTAIN,
            error_code='telegram_outcome_uncertain',
        )
        return NotificationDeliveryError(
            'Результат Telegram sendMessage неизвестен.',
            retryable=False,
        )

    def _deliver_email(
        self,
        tenant,
        event_key: str,
        email: str,
        subject: str,
        body: str,
    ) -> NotificationDeliveryError | None:
        """Retry safely with one provider idempotency key for this DB row."""
        if not email:
            return None
        fingerprint = _fingerprint_channel_payload(
            recipient=email,
            subject=subject,
            body=body,
        )
        delivery, claim_error = _claim_channel(
            tenant=tenant,
            event_key=event_key,
            channel=NotificationDelivery.Channel.EMAIL,
            payload_fingerprint=fingerprint,
        )
        if claim_error is not None:
            return claim_error
        if delivery is None:
            return None
        try:
            sent = EmailNotifier().send(
                email,
                subject,
                body,
                idempotency_key=f'map-notification/{delivery.pk}',
                message_date=delivery.created_at,
            )
        except Exception:
            sent = False
        if sent:
            _finish_channel(delivery.pk, status=NotificationDelivery.Status.SENT)
            return None
        _finish_channel(
            delivery.pk,
            status=NotificationDelivery.Status.PENDING,
            error_code='email_transport_error',
        )
        return NotificationDeliveryError(
            'Email transport failed inside the provider idempotency window.',
            retryable=True,
        )
