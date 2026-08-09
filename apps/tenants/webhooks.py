import logging

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.tenants.models import WebhookDelivery, WebhookEndpoint, WebhookEvent
from apps.tenants.webhook_limits import webhook_endpoint_quota

logger = logging.getLogger(__name__)


def _dispatch_event_safely(event_id) -> None:
    """Outbox остаётся в БД, даже если broker временно недоступен."""
    try:
        from apps.tenants.tasks import dispatch_webhook_event_task
        dispatch_webhook_event_task.delay(str(event_id))
    except Exception:
        logger.exception('Не удалось немедленно поставить webhook event=%s в очередь', event_id)


def enqueue_webhook_event(
    tenant,
    event_type: str,
    payload: dict,
    *,
    idempotency_key: str = '',
) -> WebhookEvent | None:
    """Атомарно создаёт outbox-событие и доставки для подписанных endpoint-ов."""
    fanout_limit = webhook_endpoint_quota()
    endpoints = list(
        WebhookEndpoint.objects.filter(
            tenant=tenant,
            is_active=True,
            events__contains=[event_type],
        ).order_by('pk')[:fanout_limit + 1],
    )
    if len(endpoints) > fanout_limit:
        logger.error(
            'Webhook tenant=%s exceeds endpoint quota; fan-out capped at %d',
            tenant.pk,
            fanout_limit,
        )
        endpoints = endpoints[:fanout_limit]
    if not endpoints:
        return None

    try:
        with transaction.atomic():
            event = WebhookEvent.objects.create(
                tenant=tenant,
                event_type=event_type,
                payload=payload,
                idempotency_key=idempotency_key,
            )
            WebhookDelivery.objects.bulk_create([
                WebhookDelivery(
                    event=event,
                    endpoint=endpoint,
                    endpoint_url=endpoint.url,
                    max_attempts=settings.WEBHOOK_MAX_ATTEMPTS,
                )
                for endpoint in endpoints
            ])
            transaction.on_commit(lambda: _dispatch_event_safely(event.id))
            return event
    except IntegrityError:
        if not idempotency_key:
            raise
        return WebhookEvent.objects.filter(
            tenant=tenant,
            idempotency_key=idempotency_key,
        ).first()
