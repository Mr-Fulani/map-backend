import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(queue='billing')
def billing_check_expired():
    """
    Проверяет просроченные подписки и применяет grace period логику.

    Запускается ежедневно в 10:00 через Celery Beat.
    Шаг 1: trial/active с истёкшим периодом → past_due + уведомление.
    Шаг 2: past_due дольше GRACE_PERIOD_DAYS → cancelled + уведомление.
    """
    from apps.billing.services import BillingService, GRACE_PERIOD_DAYS
    from apps.billing.models import Subscription
    from apps.notifications.services import LEVEL_BILLING, LEVEL_CRITICAL
    from apps.notifications.tasks import send_notification_task

    today = timezone.localdate()
    past_due_ids = list(
        Subscription.objects.filter(
            status__in=(Subscription.STATUS_TRIAL, Subscription.STATUS_ACTIVE),
            current_period_end__lt=today,
        ).values_list('pk', flat=True)
    )
    grace_deadline = today - timedelta(days=GRACE_PERIOD_DAYS)
    cancelled_ids = list(
        Subscription.objects.filter(
            status=Subscription.STATUS_PAST_DUE,
            current_period_end__lt=grace_deadline,
        ).values_list('pk', flat=True)
    )

    past_due_count = BillingService.check_expired_trials()
    cancelled_count = BillingService.check_grace_period_expired()

    # Уведомляем тенантов, чьи подписки переведены в past_due
    if past_due_count:
        for sub in Subscription.objects.filter(pk__in=past_due_ids).select_related('tenant'):
            send_notification_task.delay(
                sub.tenant.pk,
                LEVEL_BILLING,
                f'Ваша подписка MAP истекла. Продлите подписку в течение {GRACE_PERIOD_DAYS} дней.',
            )

    # Уведомляем тенантов, чьи подписки отменены
    if cancelled_count:
        for sub in Subscription.objects.filter(
            pk__in=cancelled_ids,
        ).select_related('tenant'):
            send_notification_task.delay(
                sub.tenant.pk,
                LEVEL_CRITICAL,
                'Ваша подписка MAP отменена. Публикация новых объявлений заблокирована.',
            )

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
