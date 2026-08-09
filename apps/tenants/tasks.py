import hashlib
import hmac
import json
import logging
import uuid
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.url_security import REDIRECT_NONE, request_public_http_url
from apps.tenants.webhook_errors import (
    SafeWebhookDeliveryError,
    safe_webhook_delivery_error,
)
from apps.tenants.webhook_limits import webhook_dispatch_batch_size

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


def _claim_due_webhook_deliveries(
    *,
    now,
    batch_size: int,
    event_id: str | None = None,
) -> list[tuple[int, uuid.UUID]]:
    """Atomically reserve due rows before publishing their Celery messages."""
    from apps.tenants.models import WebhookDelivery

    with transaction.atomic():
        due = WebhookDelivery.objects.filter(
            Q(status=WebhookDelivery.STATUS_PENDING)
            | Q(
                status=WebhookDelivery.STATUS_RETRY,
                next_attempt_at__lte=now,
            ),
        )
        if event_id is not None:
            due = due.filter(event_id=event_id)
        deliveries = list(
            due.select_for_update(skip_locked=True)
            .order_by('created_at', 'pk')[:batch_size],
        )
        claims = []
        for delivery in deliveries:
            delivery.status = WebhookDelivery.STATUS_QUEUED
            delivery.claim_token = uuid.uuid4()
            delivery.claimed_at = now
            delivery.next_attempt_at = None
            delivery.updated_at = now
            claims.append((delivery.pk, delivery.claim_token))
        if deliveries:
            WebhookDelivery.objects.bulk_update(
                deliveries,
                ['status', 'claim_token', 'claimed_at', 'next_attempt_at', 'updated_at'],
            )
    return claims


def _release_unpublished_webhook_claim(
    delivery_id: int,
    claim_token: uuid.UUID,
) -> None:
    """Make a broker publication failure retryable without exposing broker details."""
    from apps.tenants.models import WebhookDelivery

    now = timezone.now()
    WebhookDelivery.objects.filter(
        pk=delivery_id,
        status=WebhookDelivery.STATUS_QUEUED,
        claim_token=claim_token,
    ).update(
        status=WebhookDelivery.STATUS_RETRY,
        next_attempt_at=now + _webhook_retry_delay(1),
        claim_token=None,
        claimed_at=None,
        last_error=(
            'queue_publish_error: Не удалось поставить webhook delivery в очередь.'
        ),
        updated_at=now,
    )


def _publish_webhook_claims(claims: list[tuple[int, uuid.UUID]]) -> int:
    """Publish reserved rows and safely release only the exact failed claim."""
    published = 0
    for delivery_id, claim_token in claims:
        try:
            # The claim travels with the broker message. A delayed message from an
            # older claim must not be allowed to adopt a newer database claim.
            deliver_webhook_task.delay(delivery_id, str(claim_token))
        except Exception as exc:  # broker exceptions may contain credential-bearing URLs
            logger.error(
                'Webhook delivery=%s queue publish failed exception_type=%s',
                delivery_id,
                type(exc).__name__,
            )
            _release_unpublished_webhook_claim(delivery_id, claim_token)
        else:
            published += 1
    return published


@shared_task(queue='notifications')
def dispatch_webhook_event_task(event_id: str):
    """Ставит в очередь один ограниченный batch доставок outbox-события."""
    now = timezone.now()
    batch_size = webhook_dispatch_batch_size()
    claims = _claim_due_webhook_deliveries(
        now=now,
        batch_size=batch_size,
        event_id=event_id,
    )
    return {'queued': _publish_webhook_claims(claims), 'batch_limit': batch_size}


def _start_webhook_delivery(
    delivery_id: int,
    expected_claim_token: str | uuid.UUID | None = None,
):
    """Move one queued row to delivering and return its immutable claim token."""
    from apps.tenants.models import WebhookDelivery

    parsed_claim_token = None
    if expected_claim_token is not None:
        try:
            parsed_claim_token = uuid.UUID(str(expected_claim_token))
        except (TypeError, ValueError, AttributeError):
            return None, {'status': 'stale_claim'}

    with transaction.atomic():
        try:
            delivery = WebhookDelivery.objects.select_for_update().get(pk=delivery_id)
        except WebhookDelivery.DoesNotExist:
            return None, {'status': 'missing'}
        if delivery.status in (
            WebhookDelivery.STATUS_DELIVERED,
            WebhookDelivery.STATUS_FAILED,
        ):
            return None, {'status': delivery.status}
        if delivery.status == WebhookDelivery.STATUS_DELIVERING:
            return None, {'status': 'already_delivering'}
        if parsed_claim_token is not None and (
            delivery.status != WebhookDelivery.STATUS_QUEUED
            or delivery.claim_token != parsed_claim_token
        ):
            return None, {'status': 'stale_claim'}
        if (
            parsed_claim_token is None
            and delivery.status == WebhookDelivery.STATUS_QUEUED
        ):
            # All messages emitted by the claim-aware dispatcher carry a token.
            # Refuse ambiguous ID-only messages instead of adopting a newer claim.
            return None, {'status': 'stale_claim'}
        if (
            delivery.status == WebhookDelivery.STATUS_RETRY
            and delivery.next_attempt_at is not None
            and delivery.next_attempt_at > timezone.now()
        ):
            return None, {'status': 'retry_scheduled'}

        # Pending/retry support keeps pre-deploy ID-only Celery messages compatible.
        claim_token = parsed_claim_token or uuid.uuid4()
        claimed_at = timezone.now()
        delivery.status = WebhookDelivery.STATUS_DELIVERING
        delivery.claim_token = claim_token
        delivery.claimed_at = claimed_at
        delivery.attempts += 1
        delivery.last_attempt_at = claimed_at
        delivery.save(update_fields=[
            'status', 'claim_token', 'claimed_at', 'attempts',
            'last_attempt_at', 'updated_at',
        ])
        return claim_token, None


def _finish_webhook_success(
    delivery_id: int,
    claim_token: uuid.UUID,
    response_status: int,
):
    """Complete only the worker that still owns the active DB claim."""
    from apps.tenants.models import WebhookDelivery

    finished_at = timezone.now()
    updated = WebhookDelivery.objects.filter(
        pk=delivery_id,
        status=WebhookDelivery.STATUS_DELIVERING,
        claim_token=claim_token,
    ).update(
        status=WebhookDelivery.STATUS_DELIVERED,
        delivered_at=finished_at,
        next_attempt_at=None,
        claim_token=None,
        claimed_at=None,
        response_status=response_status,
        response_body='',
        last_error='',
        updated_at=finished_at,
    )
    if not updated:
        return {'status': 'stale_claim'}
    return {'status': 'delivered', 'http_status': response_status}


@shared_task(queue='notifications', soft_time_limit=60, time_limit=75)
def deliver_webhook_task(
    delivery_id: int,
    expected_claim_token: str | None = None,
):
    """Доставляет webhook один раз; retry-состояние надёжно сохраняется в БД."""
    from apps.tenants.models import WebhookDelivery

    claim_token, terminal_result = _start_webhook_delivery(
        delivery_id,
        expected_claim_token,
    )
    if terminal_result is not None:
        return terminal_result

    delivery = WebhookDelivery.objects.select_related('event', 'endpoint').get(pk=delivery_id)

    endpoint = delivery.endpoint
    if endpoint is None or endpoint.is_deleted or not endpoint.is_active:
        return _finish_webhook_failure(
            delivery_id,
            claim_token,
            SafeWebhookDeliveryError(
                'endpoint_unavailable',
                'Webhook endpoint удалён или отключён.',
            ),
            permanent=True,
        )
    try:
        body = json.dumps({
            'id': str(delivery.event_id),
            'type': delivery.event.event_type,
            'created_at': delivery.event.created_at.isoformat(),
            'data': delivery.event.payload,
        }, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()
        signature = hmac.new(
            endpoint.get_secret().encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        response = request_public_http_url(
            delivery.endpoint_url,
            method='POST',
            data=body,
            headers={
                'Content-Type': 'application/json',
                'User-Agent': 'MAP-Webhook/1.0',
                'X-MAP-Event': delivery.event.event_type,
                'X-MAP-Delivery': str(delivery.event_id),
                'X-MAP-Signature': f'sha256={signature}',
            },
            timeout=(5, settings.WEBHOOK_REQUEST_TIMEOUT_SECONDS),
            status_only=True,
            redirect_policy=REDIRECT_NONE,
        )
        if 200 <= response.status_code < 300:
            return _finish_webhook_success(
                delivery_id,
                claim_token,
                response.status_code,
            )
        return _finish_webhook_failure(
            delivery_id,
            claim_token,
            SafeWebhookDeliveryError(
                'http_error',
                f'Webhook endpoint вернул HTTP {response.status_code}.',
            ),
            response_status=response.status_code,
            response_body='',
        )
    except Exception as exc:
        safe_error = safe_webhook_delivery_error(exc)
        logger.warning(
            'Webhook delivery=%s failed code=%s exception_type=%s',
            delivery_id,
            safe_error.code,
            type(exc).__name__,
        )
        return _finish_webhook_failure(delivery_id, claim_token, safe_error)


def _finish_webhook_failure(
    delivery_id: int,
    claim_token: uuid.UUID,
    error: SafeWebhookDeliveryError,
    *,
    permanent: bool = False,
    response_status: int | None = None,
    response_body: str = '',
):
    from apps.tenants.models import WebhookDelivery

    with transaction.atomic():
        delivery = WebhookDelivery.objects.select_for_update().filter(
            pk=delivery_id,
            status=WebhookDelivery.STATUS_DELIVERING,
            claim_token=claim_token,
        ).first()
        if delivery is None:
            return {'status': 'stale_claim'}
        exhausted = permanent or delivery.attempts >= delivery.max_attempts
        delivery.status = (
            WebhookDelivery.STATUS_FAILED if exhausted else WebhookDelivery.STATUS_RETRY
        )
        delivery.next_attempt_at = None if exhausted else timezone.now() + _webhook_retry_delay(
            delivery.attempts,
        )
        delivery.last_error = error.persisted_message[:2000]
        delivery.response_status = response_status
        delivery.response_body = response_body[:2000]
        delivery.claim_token = None
        delivery.claimed_at = None
        delivery.save(update_fields=[
            'status', 'next_attempt_at', 'last_error', 'response_status',
            'response_body', 'claim_token', 'claimed_at', 'updated_at',
        ])
    return {'status': delivery.status, 'attempts': delivery.attempts}


@shared_task(queue='notifications')
def dispatch_pending_webhooks():
    """Подбирает due deliveries и восстанавливает застрявшие worker-claims."""
    from apps.tenants.models import WebhookDelivery

    now = timezone.now()
    stale_before = now - timedelta(minutes=15)
    WebhookDelivery.objects.filter(
        Q(claimed_at__lt=stale_before)
        | Q(claimed_at__isnull=True, updated_at__lt=stale_before),
        status__in=[
            WebhookDelivery.STATUS_QUEUED,
            WebhookDelivery.STATUS_DELIVERING,
        ],
    ).update(
        status=WebhookDelivery.STATUS_RETRY,
        next_attempt_at=now,
        claim_token=None,
        claimed_at=None,
        last_error=(
            'stale_claim_recovered: Предыдущая попытка webhook была безопасно перезапущена.'
        ),
        updated_at=now,
    )
    batch_size = webhook_dispatch_batch_size()
    claims = _claim_due_webhook_deliveries(now=now, batch_size=batch_size)
    return {'queued': _publish_webhook_claims(claims), 'batch_limit': batch_size}
