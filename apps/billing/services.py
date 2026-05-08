import logging
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from apps.billing.models import Invoice, Plan, Subscription
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

TRIAL_DAYS = 14
GRACE_PERIOD_DAYS = 7


class LimitChecker:
    """Проверяет лимиты тарифного плана перед выполнением операций."""

    def can_publish(self, tenant: Tenant) -> tuple[bool, str]:
        """Проверяет, можно ли опубликовать новое объявление."""
        sub = self._get_subscription(tenant)
        if sub is None or not sub.is_active:
            return False, 'Подписка неактивна.'

        plan = sub.plan
        if plan.limit_listings is not None and tenant.active_listings_count >= plan.limit_listings:
            return False, (
                f'Достигнут лимит {plan.limit_listings} активных объявлений. '
                f'Upgrade to {self._next_plan(plan)}.'
            )
        return True, ''

    def can_import_sku(self, tenant: Tenant, count: int = 1) -> tuple[bool, str]:
        """Проверяет, можно ли импортировать товары (лимит SKU в каталоге)."""
        sub = self._get_subscription(tenant)
        if sub is None or not sub.is_active:
            return False, 'Подписка неактивна.'

        plan = sub.plan
        if plan.limit_sku is not None and (tenant.sku_count + count) > plan.limit_sku:
            return False, f'Достигнут лимит {plan.limit_sku} SKU в каталоге.'
        return True, ''

    def can_generate_ai(self, tenant: Tenant) -> tuple[bool, str]:
        """Проверяет, остались ли AI-кредиты."""
        sub = self._get_subscription(tenant)
        if sub is None or not sub.is_active:
            return False, 'Подписка неактивна.'

        plan = sub.plan
        if plan.limit_ai_credits is not None and tenant.ai_credits_used >= plan.limit_ai_credits:
            return False, f'AI-кредиты исчерпаны ({tenant.ai_credits_used}/{plan.limit_ai_credits}).'
        return True, ''

    def get_usage_summary(self, tenant: Tenant) -> dict:
        """Возвращает текущее использование лимитов тенантом."""
        sub = self._get_subscription(tenant)
        plan = sub.plan if sub else None

        return {
            'listings': {
                'used': tenant.active_listings_count,
                'limit': plan.limit_listings if plan else None,
            },
            'sku': {
                'used': tenant.sku_count,
                'limit': plan.limit_sku if plan else None,
            },
            'ai_credits': {
                'used': tenant.ai_credits_used,
                'limit': plan.limit_ai_credits if plan else None,
            },
            'subscription_status': sub.status if sub else None,
            'plan': plan.slug if plan else None,
        }

    def _get_subscription(self, tenant: Tenant) -> 'Subscription | None':
        try:
            return tenant.subscription
        except Subscription.DoesNotExist:
            return None

    def _next_plan(self, plan: Plan) -> str:
        ORDER = [Plan.SLUG_STARTER, Plan.SLUG_BUSINESS, Plan.SLUG_PRO, Plan.SLUG_ENTERPRISE]
        try:
            idx = ORDER.index(plan.slug)
            return ORDER[idx + 1].capitalize() if idx + 1 < len(ORDER) else 'Enterprise'
        except ValueError:
            return 'Business'


class BillingService:
    """Управление подписками и платёжными событиями."""

    @staticmethod
    @transaction.atomic
    def start_trial(tenant: Tenant) -> Subscription:
        """Запускает 14-дневный пробный период на плане Business."""
        plan = Plan.objects.get(slug=Plan.SLUG_BUSINESS)
        today = date.today()

        subscription = Subscription.objects.create(
            tenant=tenant,
            plan=plan,
            status=Subscription.STATUS_TRIAL,
            billing_period=Subscription.PERIOD_MONTHLY,
            current_period_start=today,
            current_period_end=today + timedelta(days=TRIAL_DAYS),
        )
        logger.info('Trial запущен для тенанта %s, план %s', tenant.slug, plan.slug)
        return subscription

    @staticmethod
    @transaction.atomic
    def upgrade_plan(tenant: Tenant, plan_slug: str, period: str) -> Subscription:
        """Меняет тарифный план тенанта."""
        plan = Plan.objects.get(slug=plan_slug, is_active=True)
        today = date.today()

        if period == Subscription.PERIOD_YEARLY:
            end = today.replace(year=today.year + 1)
        else:
            next_month = today.replace(day=1) + timedelta(days=32)
            end = next_month.replace(day=today.day)

        sub = tenant.subscription
        sub.plan = plan
        sub.billing_period = period
        sub.status = Subscription.STATUS_ACTIVE
        sub.current_period_start = today
        sub.current_period_end = end
        sub.save()

        logger.info('Тенант %s перешёл на план %s', tenant.slug, plan.slug)
        return sub

    @staticmethod
    @transaction.atomic
    def handle_payment_success(tenant: Tenant, yookassa_payment_id: str, amount) -> Invoice:
        """Обрабатывает успешный платёж: активирует подписку, создаёт Invoice."""
        invoice = Invoice.objects.create(
            tenant=tenant,
            amount=amount,
            status=Invoice.STATUS_PAID,
            yookassa_payment_id=yookassa_payment_id,
            paid_at=timezone.now(),
        )
        sub = tenant.subscription
        sub.status = Subscription.STATUS_ACTIVE
        sub.save(update_fields=['status'])

        logger.info('Платёж %s прошёл для тенанта %s', yookassa_payment_id, tenant.slug)
        return invoice

    @staticmethod
    @transaction.atomic
    def handle_payment_failed(tenant: Tenant, yookassa_payment_id: str, amount) -> Invoice:
        """Обрабатывает неуспешный платёж."""
        invoice = Invoice.objects.create(
            tenant=tenant,
            amount=amount,
            status=Invoice.STATUS_FAILED,
            yookassa_payment_id=yookassa_payment_id,
        )
        logger.warning('Платёж %s не прошёл для тенанта %s', yookassa_payment_id, tenant.slug)
        return invoice

    @staticmethod
    def check_expired_trials() -> int:
        """
        Переводит просроченные trial-подписки в past_due.
        Вызывается Celery Beat ежедневно.
        Возвращает количество обработанных подписок.
        """
        today = date.today()
        expired = Subscription.objects.filter(
            status=Subscription.STATUS_TRIAL,
            current_period_end__lt=today,
        )
        count = expired.update(status=Subscription.STATUS_PAST_DUE)
        if count:
            logger.warning('Переведено %d trial-подписок в past_due', count)
        return count

    @staticmethod
    def check_grace_period_expired() -> int:
        """
        Отменяет подписки, у которых истёк grace period (7 дней past_due).
        Вызывается Celery Beat ежедневно.
        """
        deadline = date.today() - timedelta(days=GRACE_PERIOD_DAYS)
        expired = Subscription.objects.filter(
            status=Subscription.STATUS_PAST_DUE,
            current_period_end__lt=deadline,
        )
        count = expired.update(
            status=Subscription.STATUS_CANCELLED,
            cancelled_at=timezone.now(),
        )
        if count:
            logger.warning('Отменено %d подписок после grace period', count)
        return count
