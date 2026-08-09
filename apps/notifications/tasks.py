from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils.timezone import now


@shared_task(bind=True, max_retries=3, default_retry_delay=60, queue='notifications')
def send_notification_task(self, tenant_id: int, level: str, message: str, payload: dict = None):
    """
    Асинхронно отправляет уведомление тенанту через NotificationService.

    Используется везде, где нужно уведомить тенанта не задерживая основной поток.
    """
    from apps.tenants.models import Tenant
    from apps.notifications.services import NotificationService

    try:
        tenant = Tenant.objects.get(pk=tenant_id)
    except Tenant.DoesNotExist:
        return

    try:
        NotificationService().notify(tenant, level, message, payload or {})
    except Exception as exc:
        raise self.retry(exc=exc)


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
