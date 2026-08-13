import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta
from functools import partial

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.billing.models import BillingOutboxEvent, Invoice


logger = logging.getLogger(__name__)

_NOTIFICATION_LEVELS = frozenset({'billing', 'critical', 'error', 'success'})


class BillingOutboxConflictError(RuntimeError):
    """An idempotency key was reused for a different side effect."""


@dataclass(slots=True)
class BillingOutboxStats:
    claimed: int = 0
    dispatched: int = 0
    retryable: int = 0
    dead_lettered: int = 0
    skipped: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def billing_outbox_delay_seconds(attempt: int) -> int:
    exponent = min(max(0, attempt - 1), 16)
    return min(
        settings.BILLING_OUTBOX_MAX_DELAY_SECONDS,
        settings.BILLING_OUTBOX_BASE_DELAY_SECONDS * (2 ** exponent),
    )


def _kick_dispatcher_safely(event_id: uuid.UUID) -> None:
    """Best-effort fast path; the periodic dispatcher remains authoritative."""
    try:
        from apps.billing.tasks import dispatch_billing_outbox

        dispatch_billing_outbox.delay(event_ids=[str(event_id)])
    except Exception as exc:
        # Broker exceptions can contain credential-bearing connection URLs.
        logger.error(
            'Не удалось немедленно запустить billing outbox event=%s; '
            'событие осталось в БД; error_type=%s.',
            event_id,
            type(exc).__name__,
        )


def _validate_enqueue(
    *,
    tenant_id: int,
    event_type: str,
    idempotency_key: str,
    payload: dict,
    invoice: Invoice | None,
) -> None:
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            'Billing outbox должен создаваться внутри финансовой транзакции.',
        )
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError('Billing outbox idempotency_key обязателен.')
    if len(idempotency_key) > 200:
        raise ValueError('Billing outbox idempotency_key слишком длинный.')
    if event_type not in {
        BillingOutboxEvent.EVENT_NOTIFICATION,
        BillingOutboxEvent.EVENT_REQUEUE_LIMIT_REACHED,
    }:
        raise ValueError('Неизвестный тип billing outbox event.')
    if not isinstance(payload, dict):
        raise ValueError('Billing outbox payload должен быть объектом.')
    if invoice is not None and invoice.tenant_id != tenant_id:
        raise ValueError('Invoice и billing outbox принадлежат разным тенантам.')


def _enqueue(
    *,
    tenant,
    event_type: str,
    idempotency_key: str,
    payload: dict,
    invoice: Invoice | None = None,
) -> BillingOutboxEvent:
    normalized_key = idempotency_key.strip() if isinstance(idempotency_key, str) else ''
    _validate_enqueue(
        tenant_id=tenant.pk,
        event_type=event_type,
        idempotency_key=normalized_key,
        payload=payload,
        invoice=invoice,
    )
    event, created = BillingOutboxEvent.objects.get_or_create(
        tenant_id=tenant.pk,
        idempotency_key=normalized_key,
        defaults={
            'invoice': invoice,
            'event_type': event_type,
            'payload': payload,
        },
    )
    if (
        event.event_type != event_type
        or event.invoice_id != (invoice.pk if invoice is not None else None)
        or event.payload != payload
    ):
        raise BillingOutboxConflictError(
            'Billing outbox idempotency_key уже связан с другим событием.',
        )
    if created:
        transaction.on_commit(
            partial(_kick_dispatcher_safely, event.pk),
            robust=True,
        )
    return event


def enqueue_notification(
    *,
    tenant,
    level: str,
    message: str,
    idempotency_key: str,
    invoice: Invoice | None = None,
    payload: dict | None = None,
) -> BillingOutboxEvent:
    if level not in _NOTIFICATION_LEVELS:
        raise ValueError('Некорректный уровень billing-уведомления.')
    if not isinstance(message, str) or not message.strip() or len(message) > 2000:
        raise ValueError('Некорректный текст billing-уведомления.')
    notification_payload = {} if payload is None else payload
    if not isinstance(notification_payload, dict):
        raise ValueError('Payload billing-уведомления должен быть объектом.')
    return _enqueue(
        tenant=tenant,
        invoice=invoice,
        event_type=BillingOutboxEvent.EVENT_NOTIFICATION,
        idempotency_key=idempotency_key,
        payload={
            'schema': 1,
            'level': level,
            'message': message.strip(),
            'payload': notification_payload,
        },
    )


def enqueue_limit_reached_requeue(
    *,
    tenant,
    idempotency_key: str,
    invoice: Invoice | None = None,
) -> BillingOutboxEvent:
    return _enqueue(
        tenant=tenant,
        invoice=invoice,
        event_type=BillingOutboxEvent.EVENT_REQUEUE_LIMIT_REACHED,
        idempotency_key=idempotency_key,
        payload={'schema': 1},
    )


def _candidate_ids(
    *,
    limit: int,
    event_ids: list[uuid.UUID] | None,
    force: bool,
) -> list[uuid.UUID]:
    queryset = BillingOutboxEvent.objects.all()
    if event_ids is not None:
        if not event_ids:
            return []
        queryset = queryset.filter(pk__in=event_ids)
    else:
        now = timezone.now()
        stale_before = now - timedelta(
            seconds=settings.BILLING_OUTBOX_PROCESSING_TIMEOUT_SECONDS,
        )
        due_pending = Q(status=BillingOutboxEvent.STATUS_PENDING)
        if not force:
            due_pending &= (
                Q(next_attempt_at__isnull=True)
                | Q(next_attempt_at__lte=now)
            )
        queryset = queryset.filter(
            due_pending
            | Q(
                status=BillingOutboxEvent.STATUS_PROCESSING,
                processing_started_at__lt=stale_before,
            )
            | Q(
                status=BillingOutboxEvent.STATUS_PROCESSING,
                processing_started_at__isnull=True,
            )
        )
    return list(
        queryset.order_by('created_at', 'pk').values_list('pk', flat=True)[:limit],
    )


@transaction.atomic
def _claim_event(
    event_id: uuid.UUID,
    *,
    force: bool,
) -> tuple[BillingOutboxEvent, uuid.UUID | None, str]:
    event = BillingOutboxEvent.objects.select_for_update().get(pk=event_id)
    if event.status == BillingOutboxEvent.STATUS_DISPATCHED:
        return event, None, 'final'
    if event.status == BillingOutboxEvent.STATUS_DEAD and not force:
        return event, None, 'final'

    now = timezone.now()
    stale_before = now - timedelta(
        seconds=settings.BILLING_OUTBOX_PROCESSING_TIMEOUT_SECONDS,
    )
    if (
        event.status == BillingOutboxEvent.STATUS_PROCESSING
        and event.processing_started_at is not None
        and event.processing_started_at >= stale_before
    ):
        return event, None, 'busy'
    if (
        event.status == BillingOutboxEvent.STATUS_PENDING
        and not force
        and event.next_attempt_at is not None
        and event.next_attempt_at > now
    ):
        return event, None, 'waiting'

    token = uuid.uuid4()
    event.status = BillingOutboxEvent.STATUS_PROCESSING
    event.processing_token = token
    event.processing_started_at = now
    event.attempts += 1
    event.save(update_fields=[
        'status', 'processing_token', 'processing_started_at', 'attempts',
        'updated_at',
    ])
    return event, token, 'claimed'


def _publish_event(event: BillingOutboxEvent) -> None:
    task_id = f'billing-outbox-{event.pk}'
    if event.event_type == BillingOutboxEvent.EVENT_NOTIFICATION:
        payload = event.payload or {}
        if (
            payload.get('schema') != 1
            or payload.get('level') not in _NOTIFICATION_LEVELS
            or not isinstance(payload.get('message'), str)
            or not isinstance(payload.get('payload'), dict)
        ):
            raise ValueError('Повреждён payload billing-уведомления.')
        from apps.notifications.tasks import send_notification_task

        send_notification_task.apply_async(
            args=[
                event.tenant_id,
                payload['level'],
                payload['message'],
                payload['payload'],
                f'billing-outbox:{event.pk}',
            ],
            task_id=task_id,
        )
        return
    if event.event_type == BillingOutboxEvent.EVENT_REQUEUE_LIMIT_REACHED:
        if event.payload != {'schema': 1}:
            raise ValueError('Повреждён payload requeue billing outbox.')
        from apps.marketplaces.tasks import requeue_limit_reached_listings

        requeue_limit_reached_listings.apply_async(
            args=[event.tenant_id],
            task_id=task_id,
        )
        return
    raise ValueError('Неизвестный тип billing outbox event.')


@transaction.atomic
def _mark_dispatched(event_id: uuid.UUID, token: uuid.UUID) -> bool:
    event = BillingOutboxEvent.objects.select_for_update().get(pk=event_id)
    if (
        event.status != BillingOutboxEvent.STATUS_PROCESSING
        or event.processing_token != token
    ):
        return False
    event.status = BillingOutboxEvent.STATUS_DISPATCHED
    event.processing_token = None
    event.processing_started_at = None
    event.next_attempt_at = None
    event.last_error = ''
    event.dispatched_at = timezone.now()
    event.dead_lettered_at = None
    event.save(update_fields=[
        'status', 'processing_token', 'processing_started_at',
        'next_attempt_at', 'last_error', 'dispatched_at', 'dead_lettered_at',
        'updated_at',
    ])
    return True


@transaction.atomic
def _reschedule(
    event_id: uuid.UUID,
    token: uuid.UUID,
    exc: Exception,
) -> str | None:
    event = BillingOutboxEvent.objects.select_for_update().get(pk=event_id)
    if (
        event.status != BillingOutboxEvent.STATUS_PROCESSING
        or event.processing_token != token
    ):
        return None
    exhausted = event.attempts >= settings.BILLING_OUTBOX_MAX_ATTEMPTS
    event.status = (
        BillingOutboxEvent.STATUS_DEAD
        if exhausted
        else BillingOutboxEvent.STATUS_PENDING
    )
    event.processing_token = None
    event.processing_started_at = None
    event.next_attempt_at = (
        None
        if exhausted
        else timezone.now() + timedelta(
            seconds=billing_outbox_delay_seconds(event.attempts),
        )
    )
    event.dead_lettered_at = timezone.now() if exhausted else None
    # Broker errors may contain credential-bearing URLs; persist only the type.
    event.last_error = f'{type(exc).__name__}: dispatch failed'[:500]
    event.save(update_fields=[
        'status', 'processing_token', 'processing_started_at',
        'next_attempt_at', 'dead_lettered_at', 'last_error', 'updated_at',
    ])
    return 'dead' if exhausted else 'retryable'


def dispatch_due_billing_outbox(
    *,
    limit: int | None = None,
    event_ids: list[uuid.UUID] | None = None,
    force: bool = False,
) -> dict[str, int]:
    if force and event_ids is None:
        raise ValueError(
            'Принудительная отправка разрешена только для явно указанных event_ids.',
        )
    batch_limit = min(
        1000,
        max(1, limit or settings.BILLING_OUTBOX_BATCH_SIZE),
    )
    stats = BillingOutboxStats()
    for event_id in _candidate_ids(
        limit=batch_limit,
        event_ids=event_ids,
        force=force,
    ):
        try:
            event, token, state = _claim_event(event_id, force=force)
        except BillingOutboxEvent.DoesNotExist:
            stats.skipped += 1
            continue
        if state != 'claimed' or token is None:
            stats.skipped += 1
            continue
        stats.claimed += 1
        try:
            _publish_event(event)
        except Exception as exc:
            # Do not leak broker URLs or credentials through exception text.
            logger.error(
                'Billing outbox event=%s не отправлен; error_type=%s.',
                event.pk,
                type(exc).__name__,
            )
            try:
                reschedule_state = _reschedule(event.pk, token, exc)
                if reschedule_state == 'retryable':
                    stats.retryable += 1
                elif reschedule_state == 'dead':
                    stats.dead_lettered += 1
            except BillingOutboxEvent.DoesNotExist:
                stats.skipped += 1
            stats.errors += 1
            continue
        try:
            if _mark_dispatched(event.pk, token):
                stats.dispatched += 1
            else:
                stats.skipped += 1
        except BillingOutboxEvent.DoesNotExist:
            stats.skipped += 1
    return stats.to_dict()
