import logging
import calendar
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from apps.billing.models import Invoice, Plan, Subscription
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

TRIAL_DAYS = 14


def _write_billing_log(tenant, status: str, message: str) -> None:
    """Записывает биллинговое событие в SyncLog — не падает при ошибках."""
    try:
        from apps.sync.models import SyncLog
        SyncLog.objects.create(
            tenant=tenant,
            event_type=SyncLog.EVENT_BILLING,
            status=status,
            message=message,
        )
    except Exception:
        pass


GRACE_PERIOD_DAYS = 7


def add_billing_month(start: date) -> date:
    """Возвращает дату через месяц, сохраняя день или последний день месяца."""
    month = start.month + 1
    year = start.year
    if month > 12:
        month = 1
        year += 1
    last_day = calendar.monthrange(year, month)[1]
    return start.replace(year=year, month=month, day=min(start.day, last_day))


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
        from apps.marketplaces.models import Listing
        from apps.products.models import Product
        from django.utils import timezone

        sub = self._get_subscription(tenant)
        plan = sub.plan if sub else None

        # Вычисляем эффективный статус подписки в реальном времени,
        # не полагаясь на поле status — Celery Beat мог ещё не запуститься.
        effective_status = sub.status if sub else None
        if (
            sub
            and sub.status == sub.STATUS_TRIAL
            and sub.current_period_end
            and sub.current_period_end < timezone.now().date()
        ):
            effective_status = sub.STATUS_PAST_DUE

        rejected_count = Listing.objects.filter(
            tenant=tenant, status=Listing.STATUS_REJECTED,
        ).count()
        # Считаем активные листинги вживую, как rejected/sku — денормализованный
        # tenant.active_listings_count обновляется задачей и может отставать.
        active_listings_count = Listing.objects.filter(
            tenant=tenant, status=Listing.STATUS_ACTIVE,
        ).count()
        sku_count = Product.objects.filter(tenant=tenant).count()

        # Дней до принудительной отмены в grace period
        grace_days_left = None
        if effective_status == Subscription.STATUS_PAST_DUE and sub and sub.current_period_end:
            elapsed = (timezone.now().date() - sub.current_period_end).days
            grace_days_left = max(0, GRACE_PERIOD_DAYS - elapsed)

        current_period_days_left = None
        if (
            effective_status in (Subscription.STATUS_TRIAL, Subscription.STATUS_ACTIVE)
            and sub
            and sub.current_period_end
        ):
            current_period_days_left = max(
                0, (sub.current_period_end - timezone.now().date()).days,
            )

        return {
            'listings': {
                'used': active_listings_count,
                'limit': plan.limit_listings if plan else None,
            },
            'sku': {
                'used': sku_count,
                'limit': plan.limit_sku if plan else None,
            },
            'ai_credits': {
                'used': tenant.ai_credits_used,
                'limit': plan.limit_ai_credits if plan else None,
            },
            'rejected_listings': rejected_count,
            'subscription_status': effective_status,
            'current_period_days_left': current_period_days_left,
            'grace_days_left': grace_days_left,
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
    def create_payment(tenant: Tenant, plan_slug: str, period: str, return_url: str) -> str:
        """
        Создаёт платёж в YooKassa и возвращает URL для редиректа пользователя.

        Также создаёт Invoice в статусе pending, который будет обновлён вебхуком.
        """
        from apps.billing.yookassa_client import create_payment as yk_create

        plan = Plan.objects.get(slug=plan_slug, is_active=True)
        amount = plan.price_yearly if period == Subscription.PERIOD_YEARLY else plan.price_monthly
        period_label = 'год' if period == Subscription.PERIOD_YEARLY else 'месяц'
        description = f'MAP {plan.name} ({period_label})'

        payment_id, confirmation_url = yk_create(
            amount=amount,
            description=description,
            return_url=return_url,
            metadata={
                'tenant_id': str(tenant.pk),
                'plan_slug': plan_slug,
                'period': period,
            },
        )

        Invoice.objects.create(
            tenant=tenant,
            amount=amount,
            status=Invoice.STATUS_PENDING,
            yookassa_payment_id=payment_id,
        )
        return confirmation_url

    @staticmethod
    @transaction.atomic
    def handle_payment_success_webhook(payment_id: str, amount, metadata: dict) -> None:
        """
        Обрабатывает вебхук payment.succeeded от YooKassa.

        Обновляет существующий Invoice до статуса paid и активирует подписку.
        Отправляет email-уведомление тенанту.
        """
        from apps.notifications.services import LEVEL_BILLING
        from apps.notifications.tasks import send_notification_task

        try:
            invoice = Invoice.objects.select_related('tenant').get(yookassa_payment_id=payment_id)
        except Invoice.DoesNotExist:
            logger.warning('handle_payment_success_webhook: Invoice %s не найден', payment_id)
            return

        invoice.status = Invoice.STATUS_PAID
        invoice.paid_at = timezone.now()
        invoice.save(update_fields=['status', 'paid_at'])

        tenant = invoice.tenant
        plan_slug = metadata.get('plan_slug')
        period = metadata.get('period', Subscription.PERIOD_MONTHLY)

        if plan_slug:
            BillingService.upgrade_plan(tenant, plan_slug, period)
        else:
            sub = tenant.subscription
            sub.status = Subscription.STATUS_ACTIVE
            sub.save(update_fields=['status'])

        send_notification_task.delay(
            tenant.pk, LEVEL_BILLING,
            f'Оплата {invoice.amount}₽ прошла успешно. Подписка активирована.',
        )
        _write_billing_log(tenant, 'ok', f'Оплата {invoice.amount}₽ прошла успешно')
        logger.info('Вебхук payment.succeeded: tenant=%s, amount=%s', tenant.slug, amount)

    @staticmethod
    @transaction.atomic
    def handle_payment_failed_webhook(payment_id: str) -> None:
        """
        Обрабатывает вебхук payment.canceled от YooKassa.

        Переводит Invoice в статус failed.
        """
        invoice = Invoice.objects.filter(yookassa_payment_id=payment_id).select_related('tenant').first()
        Invoice.objects.filter(yookassa_payment_id=payment_id).update(status=Invoice.STATUS_FAILED)
        if invoice:
            _write_billing_log(invoice.tenant, 'warn', f'Оплата не прошла (payment_id={payment_id})')
        logger.warning('Вебхук payment.canceled: payment_id=%s', payment_id)

    @staticmethod
    @transaction.atomic
    def start_trial(tenant: Tenant) -> Subscription:
        """Запускает 14-дневный пробный период на плане Business."""
        plan = Plan.objects.get(slug=Plan.SLUG_BUSINESS)
        today = date.today()

        trial_end = today + timedelta(days=TRIAL_DAYS)
        subscription = Subscription.objects.create(
            tenant=tenant,
            plan=plan,
            status=Subscription.STATUS_TRIAL,
            billing_period=Subscription.PERIOD_MONTHLY,
            current_period_start=today,
            current_period_end=trial_end,
        )
        tenant.trial_ends_at = timezone.make_aware(
            timezone.datetime.combine(trial_end, timezone.datetime.min.time())
        )
        tenant.save(update_fields=['trial_ends_at'])
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
            end = add_billing_month(today)

        sub = tenant.subscription
        sub.plan = plan
        sub.billing_period = period
        sub.status = Subscription.STATUS_ACTIVE
        sub.current_period_start = today
        sub.current_period_end = end
        sub.save()

        # Новый расчётный период — сбрасываем счётчик AI-кредитов
        Tenant.objects.filter(pk=tenant.pk).update(ai_credits_used=0)

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
