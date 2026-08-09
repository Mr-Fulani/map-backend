import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(
    queue='billing',
    acks_late=True,
    reject_on_worker_lost=True,
)
def dispatch_billing_outbox(event_ids=None, limit=None, force=False):
    """Публикует сохранённые billing side effects и повторяет broker failures."""
    from apps.billing.outbox import dispatch_due_billing_outbox

    return dispatch_due_billing_outbox(
        event_ids=event_ids,
        limit=limit,
        force=force,
    )


@shared_task(queue='billing')
def reconcile_yookassa_billing():
    """Сверяет зависшие checkout intents и ошибки webhook с YooKassa API."""
    from apps.billing.reconciliation import reconcile_yookassa_billing as reconcile

    return reconcile()


@shared_task(queue='billing')
def billing_check_expired():
    """
    Проверяет просроченные подписки и применяет grace period логику.

    Запускается ежедневно в 10:00 через Celery Beat.
    Шаг 1: trial/active с истёкшим периодом → past_due + уведомление.
    Шаг 2: past_due дольше GRACE_PERIOD_DAYS → cancelled + уведомление.
    """
    from apps.billing.services import BillingService

    past_due_count = BillingService.check_expired_trials()
    cancelled_count = BillingService.check_grace_period_expired()

    logger.info(
        'billing_check_expired: past_due=%d, cancelled=%d', past_due_count, cancelled_count,
    )
    return {'past_due_updated': past_due_count, 'cancelled': cancelled_count}


@shared_task(queue='billing')
def reset_monthly_ai_credits():
    """
    Идемпотентно начисляет включённый AI-баланс для текущего расчётного периода.

    Купленный баланс не сбрасывается.
    """
    from apps.billing.models import Subscription
    from apps.billing.services import BillingService

    today = timezone.localdate()
    reset_count = 0

    active_subscription_ids = Subscription.objects.filter(
        status=Subscription.STATUS_ACTIVE,
        current_period_start__isnull=False,
        current_period_start__lte=today,
        current_period_end__gt=today,
    ).values_list('pk', flat=True)

    for subscription_id in active_subscription_ids:
        if BillingService.refresh_ai_credit_period(subscription_id, today):
            reset_count += 1

    if reset_count:
        logger.info('reset_monthly_ai_credits: сброшено кредитов для %d тенантов', reset_count)
    return {'reset_count': reset_count}
