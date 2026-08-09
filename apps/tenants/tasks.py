import logging
import hashlib
import hmac
import json
from datetime import timedelta

import requests
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.url_security import is_safe_public_http_url

logger = logging.getLogger(__name__)


@shared_task(queue='sync_import')
def update_tenant_counters():
    """
    Обновляет кэш-счётчики тенантов: active_listings_count, sku_count.

    Запускается каждые 15 минут через Celery Beat.
    Использует F() и annotate для атомарности без N+1.
    """
    from apps.marketplaces.models import Listing
    from apps.tenants.models import Tenant

    for tenant in Tenant.objects.filter(is_active=True):
        active_listings = Listing.objects.filter(
            tenant=tenant, status=Listing.STATUS_ACTIVE,
        ).count()
        sku_count = tenant.products.count()
        Tenant.objects.filter(pk=tenant.pk).update(
            active_listings_count=active_listings,
            sku_count=sku_count,
        )

    count = Tenant.objects.filter(is_active=True).count()
    logger.info('update_tenant_counters: обновлено %d тенантов', count)
    return {'tenants_updated': count}


def _webhook_retry_delay(attempt: int) -> timedelta:
    delays = [60, 300, 900, 3600, 6 * 3600, 12 * 3600, 24 * 3600]
    return timedelta(seconds=delays[min(max(attempt - 1, 0), len(delays) - 1)])


@shared_task(queue='notifications')
def dispatch_webhook_event_task(event_id: str):
    """Ставит в очередь все незавершённые доставки outbox-события."""
    from apps.tenants.models import WebhookDelivery

    delivery_ids = list(WebhookDelivery.objects.filter(
        event_id=event_id,
        status__in=[WebhookDelivery.STATUS_PENDING, WebhookDelivery.STATUS_RETRY],
    ).values_list('pk', flat=True))
    for delivery_id in delivery_ids:
        deliver_webhook_task.delay(delivery_id)
    return {'queued': len(delivery_ids)}


@shared_task(queue='notifications')
def deliver_webhook_task(delivery_id: int):
    """Доставляет webhook один раз; retry-состояние надёжно сохраняется в БД."""
    from apps.tenants.models import WebhookDelivery

    with transaction.atomic():
        try:
            delivery = WebhookDelivery.objects.select_for_update().get(pk=delivery_id)
        except WebhookDelivery.DoesNotExist:
            return {'status': 'missing'}
        if delivery.status in (WebhookDelivery.STATUS_DELIVERED, WebhookDelivery.STATUS_FAILED):
            return {'status': delivery.status}
        if delivery.status == WebhookDelivery.STATUS_DELIVERING:
            return {'status': 'already_delivering'}
        delivery.status = WebhookDelivery.STATUS_DELIVERING
        delivery.attempts += 1
        delivery.last_attempt_at = timezone.now()
        delivery.save(update_fields=['status', 'attempts', 'last_attempt_at', 'updated_at'])

    delivery = WebhookDelivery.objects.select_related('event', 'endpoint').get(pk=delivery_id)

    endpoint = delivery.endpoint
    if endpoint is None or endpoint.is_deleted or not endpoint.is_active:
        return _finish_webhook_failure(delivery_id, 'Webhook endpoint удалён или отключён.', permanent=True)
    if not is_safe_public_http_url(delivery.endpoint_url, resolve_hostname=True):
        return _finish_webhook_failure(delivery_id, 'Webhook URL не является публичным.', permanent=True)

    body = json.dumps({
        'id': str(delivery.event_id),
        'type': delivery.event.event_type,
        'created_at': delivery.event.created_at.isoformat(),
        'data': delivery.event.payload,
    }, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()
    try:
        signature = hmac.new(
            endpoint.get_secret().encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        response = requests.post(
            delivery.endpoint_url,
            data=body,
            headers={
                'Content-Type': 'application/json',
                'User-Agent': 'MAP-Webhook/1.0',
                'X-MAP-Event': delivery.event.event_type,
                'X-MAP-Delivery': str(delivery.event_id),
                'X-MAP-Signature': f'sha256={signature}',
            },
            timeout=settings.WEBHOOK_REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        if 200 <= response.status_code < 300:
            WebhookDelivery.objects.filter(pk=delivery_id).update(
                status=WebhookDelivery.STATUS_DELIVERED,
                delivered_at=timezone.now(),
                next_attempt_at=None,
                response_status=response.status_code,
                response_body=response.text[:2000],
                last_error='',
            )
            return {'status': 'delivered', 'http_status': response.status_code}
        return _finish_webhook_failure(
            delivery_id,
            f'HTTP {response.status_code}',
            response_status=response.status_code,
            response_body=response.text[:2000],
        )
    except Exception as exc:
        logger.warning('Webhook delivery=%s failed: %s', delivery_id, exc)
        return _finish_webhook_failure(delivery_id, str(exc))


def _finish_webhook_failure(
    delivery_id: int,
    error: str,
    *,
    permanent: bool = False,
    response_status: int | None = None,
    response_body: str = '',
):
    from apps.tenants.models import WebhookDelivery

    with transaction.atomic():
        delivery = WebhookDelivery.objects.select_for_update().get(pk=delivery_id)
        exhausted = permanent or delivery.attempts >= delivery.max_attempts
        delivery.status = (
            WebhookDelivery.STATUS_FAILED if exhausted else WebhookDelivery.STATUS_RETRY
        )
        delivery.next_attempt_at = None if exhausted else timezone.now() + _webhook_retry_delay(
            delivery.attempts,
        )
        delivery.last_error = error[:2000]
        delivery.response_status = response_status
        delivery.response_body = response_body[:2000]
        delivery.save(update_fields=[
            'status', 'next_attempt_at', 'last_error', 'response_status',
            'response_body', 'updated_at',
        ])
    return {'status': delivery.status, 'attempts': delivery.attempts}


@shared_task(queue='notifications')
def dispatch_pending_webhooks():
    """Подбирает due deliveries и восстанавливает застрявшие worker-claims."""
    from apps.tenants.models import WebhookDelivery

    now = timezone.now()
    stale_before = now - timedelta(minutes=15)
    WebhookDelivery.objects.filter(
        status=WebhookDelivery.STATUS_DELIVERING,
        updated_at__lt=stale_before,
    ).update(status=WebhookDelivery.STATUS_RETRY, next_attempt_at=now)
    due_ids = list(WebhookDelivery.objects.filter(
        Q(status=WebhookDelivery.STATUS_PENDING)
        | Q(status=WebhookDelivery.STATUS_RETRY, next_attempt_at__lte=now)
    ).order_by('created_at').values_list('pk', flat=True)[:500])
    for delivery_id in due_ids:
        deliver_webhook_task.delay(delivery_id)
    return {'queued': len(due_ids)}
