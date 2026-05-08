import logging

from celery import shared_task
from django.utils.timezone import now

logger = logging.getLogger(__name__)


@shared_task(queue='billing')
def billing_check_expired():
    """
    Проверяет просроченные подписки и переводит их в статус past_due.

    Запускается ежедневно в 10:00 через Celery Beat.
    Детальная реализация — Этап 11 (YooKassa).
    """
    from apps.billing.models import Subscription

    expired = Subscription.objects.filter(
        status=Subscription.STATUS_ACTIVE,
        current_period_end__lt=now(),
    )
    count = expired.update(status=Subscription.STATUS_PAST_DUE)
    logger.info('billing_check_expired: переведено в past_due %d подписок', count)
    return {'past_due_updated': count}
