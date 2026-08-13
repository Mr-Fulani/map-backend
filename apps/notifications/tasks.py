from datetime import timedelta
import hashlib
import json
import logging

from celery import shared_task
from django.conf import settings
from django.utils.timezone import now


logger = logging.getLogger(__name__)
NOTIFICATION_DELIVERY_MAX_RETRIES = 6


@shared_task(
    bind=True,
    max_retries=NOTIFICATION_DELIVERY_MAX_RETRIES,
    default_retry_delay=60,
    queue='notifications',
    acks_late=True,
    reject_on_worker_lost=True,
)
def send_notification_task(
    self,
    tenant_id: int,
    level: str,
    message: str,
    payload: dict | None = None,
    event_key: str | None = None,
):
    """
    Асинхронно отправляет уведомление тенанту через NotificationService.

    Используется везде, где нужно уведомить тенанта не задерживая основной поток.
    """
    from apps.tenants.models import Tenant
    from apps.notifications.services import (
        NotificationDeliveryError,
        NotificationService,
        mark_notification_event_exhausted,
    )

    try:
        tenant = Tenant.objects.get(pk=tenant_id)
    except Tenant.DoesNotExist:
        return

    if event_key is None:
        task_id = getattr(self.request, 'id', None)
        parent_id = getattr(self.request, 'parent_id', None)
        producer_id = parent_id or task_id
        if producer_id:
            payload_digest = hashlib.sha256(json.dumps(
                {
                    'tenant_id': tenant_id,
                    'level': level,
                    'message': message,
                    'payload': payload or {},
                },
                ensure_ascii=False,
                separators=(',', ':'),
                sort_keys=True,
            ).encode('utf-8')).hexdigest()
            # A redelivered parent task can publish a new child Celery id. Its
            # parent_id remains stable, so the logical delivery key does too.
            event_key = f'notification-producer:{producer_id}:{payload_digest}'
        else:
            digest = hashlib.sha256(json.dumps(
                {
                    'tenant_id': tenant_id,
                    'level': level,
                    'message': message,
                    'payload': payload or {},
                },
                ensure_ascii=False,
                separators=(',', ':'),
                sort_keys=True,
            ).encode('utf-8')).hexdigest()
            event_key = f'direct-notification:{digest}'

    try:
        NotificationService().notify(
            tenant,
            level,
            message,
            payload or {},
            event_key=event_key,
        )
    except ValueError:
        # Invalid levels/payloads are permanent producer errors. Mark the task
        # failed immediately instead of retrying the same malformed message.
        logger.exception(
            'Permanent notification error tenant_id=%s task_id=%s',
            tenant_id,
            self.request.id,
        )
        raise
    except NotificationDeliveryError as exc:
        retry_number = getattr(self.request, 'retries', 0)
        if not exc.retryable or retry_number >= self.max_retries:
            if exc.retryable:
                mark_notification_event_exhausted(
                    tenant_id=tenant_id,
                    event_key=event_key,
                )
            logger.exception(
                'Notification delivery requires operator attention '
                'tenant_id=%s task_id=%s retryable=%s',
                tenant_id,
                self.request.id,
                exc.retryable,
            )
            raise
        raise self.retry(
            exc=exc,
            countdown=min(3600, 60 * (2 ** retry_number)),
        )
    except Exception as exc:
        retry_number = getattr(self.request, 'retries', 0)
        if retry_number >= self.max_retries:
            # Redis transport has no native dead-letter exchange. Keep a clear
            # terminal failure in worker/error monitoring after bounded retries;
            # durable producer outboxes retain their source event separately.
            logger.exception(
                'Notification delivery exhausted retries tenant_id=%s task_id=%s',
                tenant_id,
                self.request.id,
            )
            raise
        raise self.retry(
            exc=exc,
            countdown=min(3600, 60 * (2 ** retry_number)),
        )


@shared_task(queue='notifications')
def cleanup_old_logs():
    """
    Удаляет записи SyncLog старше 90 дней.

    Запускается ежедневно в 02:00 по Celery Beat.
    """
    from apps.sync.models import SyncLog

    cutoff = now() - timedelta(days=settings.SYNC_LOG_RETENTION_DAYS)
    deleted, _ = SyncLog.objects.filter(created_at__lt=cutoff).delete()
    return {'deleted': deleted}
