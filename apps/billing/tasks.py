import logging

from celery import shared_task

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

    past_due_count = BillingService.check_expired_trials()
    cancelled_count = BillingService.check_grace_period_expired()

    # Уведомляем тенантов, чьи подписки переведены в past_due
    if past_due_count:
        for sub in Subscription.objects.filter(status=Subscription.STATUS_PAST_DUE).select_related('tenant'):
            send_notification_task.delay(
                sub.tenant.pk,
                LEVEL_BILLING,
                f'Ваша подписка MAP истекла. Продлите подписку в течение {GRACE_PERIOD_DAYS} дней.',
            )

    # Уведомляем тенантов, чьи подписки отменены
    if cancelled_count:
        for sub in Subscription.objects.filter(
            status=Subscription.STATUS_CANCELLED,
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
    Сбрасывает AI-кредиты для активных подписчиков в начале нового расчётного периода.

    Запускается ежедневно. Сбрасывает кредиты тем тенантам, у которых сегодня
    совпадает день начала расчётного периода (current_period_start.day == today.day).
    """
    from datetime import date
    from apps.billing.models import Subscription
    from apps.tenants.models import Tenant

    today = date.today()
    reset_count = 0

    active_subs = Subscription.objects.filter(
        status=Subscription.STATUS_ACTIVE,
        current_period_start__isnull=False,
    ).select_related('tenant')

    for sub in active_subs:
        if sub.current_period_start.day == today.day:
            Tenant.objects.filter(pk=sub.tenant_id).update(ai_credits_used=0)
            reset_count += 1

    if reset_count:
        logger.info('reset_monthly_ai_credits: сброшено кредитов для %d тенантов', reset_count)
    return {'reset_count': reset_count}
