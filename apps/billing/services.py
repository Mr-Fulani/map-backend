import logging
import calendar
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.billing.models import (
    AICreditPackage, Invoice, PaymentReversal, Plan, Subscription,
)
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

TRIAL_DAYS = settings.BILLING_TRIAL_DAYS


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


GRACE_PERIOD_DAYS = settings.BILLING_GRACE_PERIOD_DAYS


def add_billing_month(start: date) -> date:
    """Возвращает дату через месяц, сохраняя день или последний день месяца."""
    return add_billing_months(start, 1)


def add_billing_months(anchor: date, months: int) -> date:
    """Сдвигает дату от исходного anchor без дрейфа после короткого месяца."""
    absolute_month = anchor.year * 12 + anchor.month - 1 + months
    year, zero_based_month = divmod(absolute_month, 12)
    month = zero_based_month + 1
    last_day = calendar.monthrange(year, month)[1]
    return anchor.replace(year=year, month=month, day=min(anchor.day, last_day))


def add_billing_year(start: date) -> date:
    """Возвращает дату через год; 29 февраля переводит в 28 февраля."""
    try:
        return start.replace(year=start.year + 1)
    except ValueError:
        return start.replace(year=start.year + 1, month=2, day=28)


def ai_credit_period_for_date(
    subscription: Subscription,
    target_date: date,
) -> tuple[date, date] | None:
    """Возвращает месячный AI-период внутри оплаченного периода подписки."""
    subscription_start = subscription.current_period_start
    subscription_end = subscription.current_period_end
    if target_date < subscription_start or target_date >= subscription_end:
        return None
    if subscription.billing_period != Subscription.PERIOD_YEARLY:
        return subscription_start, subscription_end

    period_start = subscription_start
    for month_index in range(1, 13):
        period_end = min(
            add_billing_months(subscription_start, month_index),
            subscription_end,
        )
        if target_date < period_end:
            return period_start, period_end
        period_start = period_end
        if period_end >= subscription_end:
            break
    return None


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
        """Проверяет активность подписки и доступный AI-баланс."""
        sub = self._get_subscription(tenant)
        if sub is None or not sub.is_active:
            return False, 'Подписка неактивна.'
        from apps.billing.ai_wallet import AIWalletService
        wallet = AIWalletService.summary(tenant)
        if wallet['available'] < 1:
            return False, 'AI-баланс исчерпан. Пополните баланс или обновите тариф.'
        return True, ''

    def get_usage_summary(self, tenant: Tenant) -> dict:
        """Возвращает текущее использование лимитов тенантом."""
        from apps.marketplaces.models import Listing
        from apps.products.models import Product
        from django.utils import timezone

        sub = self._get_subscription(tenant)
        plan = sub.plan if sub else None

        effective_status = sub.effective_status if sub else None

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
            elapsed = (timezone.localdate() - sub.current_period_end).days
            grace_days_left = max(0, GRACE_PERIOD_DAYS - elapsed)

        current_period_days_left = None
        if (
            effective_status in (Subscription.STATUS_TRIAL, Subscription.STATUS_ACTIVE)
            and sub
            and sub.current_period_end
        ):
            current_period_days_left = max(
                0, (sub.current_period_end - timezone.localdate()).days,
            )

        from apps.billing.ai_wallet import AIWalletService
        wallet = AIWalletService.summary(tenant)

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
                'used': wallet['included_used'],
                'successful_requests': tenant.ai_credits_used,
                'limit': wallet['included_limit'],
                'included_balance': wallet['included'],
                'included_percent_used': wallet['included_percent_used'],
                'purchased_balance': wallet['purchased'],
                'reserved_balance': wallet['reserved'],
                'available_balance': wallet['available'],
                'unlimited': wallet['unlimited'],
                'individual_limit': wallet['individual_limit'],
                'overage_active': wallet['overage_active'],
                'threshold': wallet['threshold'],
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
    def access_mode(tenant: Tenant) -> str:
        """Единый режим доступа тенанта для HTTP и фоновых операций."""
        if not tenant.is_active:
            return Subscription.ACCESS_BILLING_ONLY
        try:
            return tenant.subscription.access_mode
        except Subscription.DoesNotExist:
            return Subscription.ACCESS_BILLING_ONLY

    @staticmethod
    def sync_tenant_trial_end(subscription: Subscription) -> None:
        """Синхронизирует legacy-поле Tenant.trial_ends_at с подпиской."""
        trial_end = None
        if subscription.status == Subscription.STATUS_TRIAL:
            trial_end = timezone.make_aware(
                datetime.combine(
                    subscription.current_period_end,
                    time.min,
                ),
            )
        Tenant.objects.filter(pk=subscription.tenant_id).update(trial_ends_at=trial_end)

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
            currency='RUB',
            status=Invoice.STATUS_PENDING,
            yookassa_payment_id=payment_id,
            purchase_type=Invoice.TYPE_SUBSCRIPTION,
            metadata={'plan_slug': plan_slug, 'period': period},
        )
        return confirmation_url

    @staticmethod
    def create_ai_topup_payment(tenant: Tenant, package_id: int, return_url: str) -> str:
        from apps.billing.yookassa_client import create_payment as yk_create

        package = AICreditPackage.objects.get(pk=package_id, is_active=True)
        metadata = {
            'tenant_id': str(tenant.pk),
            'purchase_type': Invoice.TYPE_AI_TOPUP,
            'package_id': str(package.pk),
        }
        payment_id, confirmation_url = yk_create(
            amount=package.price_rub,
            description=f'MAP — {package.name}',
            return_url=return_url,
            metadata=metadata,
        )
        Invoice.objects.create(
            tenant=tenant,
            amount=package.price_rub,
            currency='RUB',
            status=Invoice.STATUS_PENDING,
            yookassa_payment_id=payment_id,
            purchase_type=Invoice.TYPE_AI_TOPUP,
            metadata=metadata,
        )
        return confirmation_url

    @staticmethod
    @transaction.atomic
    def handle_payment_success_webhook(
        payment_id: str,
        amount,
        metadata: dict,
        currency: str = 'RUB',
    ) -> bool:
        """
        Обрабатывает вебхук payment.succeeded от YooKassa.

        Обновляет существующий Invoice до статуса paid и активирует подписку.
        Отправляет email-уведомление тенанту.
        """
        from apps.notifications.services import LEVEL_BILLING
        from apps.notifications.tasks import send_notification_task

        try:
            invoice = Invoice.objects.select_for_update().select_related('tenant').get(
                yookassa_payment_id=payment_id,
            )
        except Invoice.DoesNotExist:
            logger.warning('handle_payment_success_webhook: Invoice %s не найден', payment_id)
            return False

        # YooKassa повторяет webhook до получения HTTP 200. Повтор уже
        # обработанного события не должен продлевать подписку или уведомлять снова.
        if invoice.status in (
            Invoice.STATUS_PAID,
            Invoice.STATUS_PARTIALLY_REFUNDED,
            Invoice.STATUS_REFUNDED,
        ):
            return True
        if invoice.status != Invoice.STATUS_PENDING:
            logger.error(
                'Нельзя применить успешный платёж к invoice=%s в статусе %s',
                invoice.pk,
                invoice.status,
            )
            return False

        try:
            received_amount = Decimal(str(amount)).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError, ValueError):
            logger.error(
                'Некорректная сумма webhook: invoice=%s payment_id=%s',
                invoice.pk, payment_id,
            )
            return False

        received_currency = str(currency or '').upper()
        if received_amount != invoice.amount or received_currency != invoice.currency:
            logger.error(
                (
                    'Webhook не совпадает со счётом: invoice=%s payment_id=%s '
                    'expected=%s %s received=%s %s'
                ),
                invoice.pk,
                payment_id,
                invoice.amount,
                invoice.currency,
                received_amount,
                received_currency or '<empty>',
            )
            _write_billing_log(
                invoice.tenant,
                'error',
                f'Платёж {payment_id} требует проверки: сумма или валюта не совпадает',
            )
            return False

        tenant = invoice.tenant
        # Решение о покупке принимается только по сохранённому Invoice.
        # Metadata из входящего webhook не является источником истины.
        stored_metadata = invoice.metadata or {}
        if invoice.purchase_type == Invoice.TYPE_AI_TOPUP:
            package = AICreditPackage.objects.filter(
                pk=stored_metadata.get('package_id'),
            ).first()
            if package is None:
                logger.error('Пакет AI-кредитов для invoice=%s не найден', invoice.pk)
                return False

        invoice.status = Invoice.STATUS_PAID
        invoice.paid_at = timezone.now()
        invoice.save(update_fields=['status', 'paid_at'])

        if invoice.purchase_type == Invoice.TYPE_AI_TOPUP:
            from apps.billing.ai_wallet import AIWalletService
            AIWalletService.topup(
                tenant,
                package.credits,
                idempotency_key=f'yookassa-topup:{payment_id}',
                reference=payment_id,
            )
            send_notification_task.delay(
                tenant.pk, LEVEL_BILLING,
                f'AI-баланс пополнен на {package.credits} кредитов.',
            )
            _write_billing_log(
                tenant, 'ok',
                f'AI-баланс пополнен на {package.credits} кредитов',
            )
            return True

        plan_slug = stored_metadata.get('plan_slug')
        period = stored_metadata.get('period', Subscription.PERIOD_MONTHLY)

        if plan_slug:
            BillingService.upgrade_plan(tenant, plan_slug, period)
        else:
            sub = tenant.subscription
            sub.status = Subscription.STATUS_ACTIVE
            sub.save(update_fields=['status'])
            BillingService.sync_tenant_trial_end(sub)

        send_notification_task.delay(
            tenant.pk, LEVEL_BILLING,
            f'Оплата {invoice.amount}₽ прошла успешно. Подписка активирована.',
        )
        BillingService._requeue_limit_reached_listings(tenant)
        _write_billing_log(tenant, 'ok', f'Оплата {invoice.amount}₽ прошла успешно')
        logger.info('Вебхук payment.succeeded: tenant=%s, amount=%s', tenant.slug, amount)
        return True

    @staticmethod
    def _requeue_limit_reached_listings(tenant: Tenant) -> None:
        """Ставит в очередь повторную публикацию листингов «Лимит достигнут».

        Вызывается после активации подписки: без этого листинги, упёршиеся
        в лимит во время неактивной подписки, остаются в тупиковом статусе.
        """
        from apps.marketplaces.tasks import requeue_limit_reached_listings

        tenant_id = tenant.pk
        transaction.on_commit(lambda: requeue_limit_reached_listings.delay(tenant_id))

    @staticmethod
    @transaction.atomic
    def handle_payment_failed_webhook(payment_id: str) -> None:
        """
        Обрабатывает вебхук payment.canceled от YooKassa.

        Переводит Invoice в статус failed.
        """
        invoice = Invoice.objects.filter(
            yookassa_payment_id=payment_id,
        ).select_related('tenant').first()
        Invoice.objects.filter(
            yookassa_payment_id=payment_id,
            status=Invoice.STATUS_PENDING,
        ).update(status=Invoice.STATUS_FAILED)
        if invoice:
            _write_billing_log(invoice.tenant, 'warn', f'Оплата не прошла (payment_id={payment_id})')
        logger.warning('Вебхук payment.canceled: payment_id=%s', payment_id)

    @staticmethod
    @transaction.atomic
    def handle_reversal_success(
        *,
        provider_reference: str,
        payment_id: str,
        amount,
        currency: str,
        kind: str = PaymentReversal.KIND_REFUND,
    ) -> PaymentReversal | None:
        """Применяет успешный refund/chargeback как неизменяемую обратную операцию."""
        existing = PaymentReversal.objects.filter(
            provider_reference=provider_reference,
        ).first()
        if existing is not None:
            return existing

        invoice = Invoice.objects.select_for_update().select_related('tenant').filter(
            yookassa_payment_id=payment_id,
        ).first()
        if invoice is None:
            logger.error('Возврат %s: invoice для payment_id=%s не найден', provider_reference, payment_id)
            return None
        existing = PaymentReversal.objects.filter(
            provider_reference=provider_reference,
        ).first()
        if existing is not None:
            return existing

        try:
            reversal_amount = Decimal(str(amount)).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError, ValueError):
            logger.error('Возврат %s: некорректная сумма %r', provider_reference, amount)
            return None

        received_currency = str(currency or '').upper()
        refundable_statuses = {
            Invoice.STATUS_PAID,
            Invoice.STATUS_PARTIALLY_REFUNDED,
            Invoice.STATUS_MANUAL_REVIEW,
        }
        if invoice.status not in refundable_statuses:
            invoice.refund_review_required = True
            invoice.status = Invoice.STATUS_MANUAL_REVIEW
            invoice.save(update_fields=[
                'refund_review_required', 'status', 'updated_at',
            ])
            return PaymentReversal.objects.create(
                invoice=invoice,
                kind=kind,
                provider_reference=provider_reference,
                payment_id=payment_id,
                amount=max(Decimal('0'), reversal_amount),
                currency=received_currency or invoice.currency,
                status=PaymentReversal.STATUS_MANUAL_REVIEW,
                reason='Исходный Invoice не находился в оплачиваемом статусе.',
            )

        if reversal_amount <= 0 or received_currency != invoice.currency:
            invoice.refund_review_required = True
            invoice.status = Invoice.STATUS_MANUAL_REVIEW
            invoice.save(update_fields=[
                'refund_review_required', 'status', 'updated_at',
            ])
            return PaymentReversal.objects.create(
                invoice=invoice,
                kind=kind,
                provider_reference=provider_reference,
                payment_id=payment_id,
                amount=max(Decimal('0'), reversal_amount),
                currency=received_currency or invoice.currency,
                status=PaymentReversal.STATUS_MANUAL_REVIEW,
                reason='Некорректная сумма или валюта возврата.',
            )

        remaining_refundable = invoice.amount - invoice.refunded_amount
        if reversal_amount > remaining_refundable:
            invoice.refund_review_required = True
            invoice.status = Invoice.STATUS_MANUAL_REVIEW
            invoice.save(update_fields=[
                'refund_review_required', 'status', 'updated_at',
            ])
            return PaymentReversal.objects.create(
                invoice=invoice,
                kind=kind,
                provider_reference=provider_reference,
                payment_id=payment_id,
                amount=reversal_amount,
                currency=received_currency,
                status=PaymentReversal.STATUS_MANUAL_REVIEW,
                reason='Сумма возвратов превышает сумму исходного платежа.',
            )

        new_refunded_amount = invoice.refunded_amount + reversal_amount
        reversal = PaymentReversal.objects.create(
            invoice=invoice,
            kind=kind,
            provider_reference=provider_reference,
            payment_id=payment_id,
            amount=reversal_amount,
            currency=received_currency,
            status=PaymentReversal.STATUS_MANUAL_REVIEW,
        )

        if invoice.purchase_type != Invoice.TYPE_AI_TOPUP:
            reversal.reason = (
                'Возврат подписки требует ручной проверки периода и объёма '
                'оказанной услуги.'
            )
            reversal.save(update_fields=['reason', 'updated_at'])
            invoice.refunded_amount = new_refunded_amount
            invoice.refund_review_required = True
            invoice.status = Invoice.STATUS_MANUAL_REVIEW
            invoice.save(update_fields=[
                'refunded_amount', 'refund_review_required', 'status', 'updated_at',
            ])
            return reversal

        package = AICreditPackage.objects.filter(
            pk=(invoice.metadata or {}).get('package_id'),
        ).first()
        if package is None:
            reversal.reason = 'Пакет кредитов исходного платежа не найден.'
            reversal.save(update_fields=['reason', 'updated_at'])
            invoice.refunded_amount = new_refunded_amount
            invoice.refund_review_required = True
            invoice.status = Invoice.STATUS_MANUAL_REVIEW
            invoice.save(update_fields=[
                'refunded_amount', 'refund_review_required', 'status', 'updated_at',
            ])
            return reversal

        previous_requested = (
            PaymentReversal.objects.filter(invoice=invoice)
            .exclude(pk=reversal.pk)
            .aggregate(total=Sum('credits_requested'))['total']
            or Decimal('0')
        )
        if new_refunded_amount == invoice.amount:
            cumulative_credit_target = package.credits
        else:
            cumulative_credit_target = (
                package.credits * new_refunded_amount / invoice.amount
            ).quantize(Decimal('0.0001'), rounding=ROUND_DOWN)
        credits_requested = max(
            Decimal('0'),
            cumulative_credit_target - previous_requested,
        )

        from apps.billing.ai_wallet import AIWalletService
        transaction_kind = (
            'chargeback'
            if kind == PaymentReversal.KIND_CHARGEBACK
            else 'refund'
        )
        credits_reversed, shortfall = AIWalletService.reverse_purchased(
            invoice.tenant,
            credits_requested,
            idempotency_key=f'{transaction_kind}:{provider_reference}',
            reference=provider_reference,
            kind=transaction_kind,
        )

        reversal.credits_requested = credits_requested
        reversal.credits_reversed = credits_reversed
        reversal.credit_shortfall = shortfall
        invoice.refunded_amount = new_refunded_amount
        if shortfall:
            reversal.status = PaymentReversal.STATUS_MANUAL_REVIEW
            reversal.reason = (
                f'Не удалось отозвать {shortfall} уже потраченных или '
                'зарезервированных кредитов.'
            )
            invoice.refund_review_required = True
            invoice.status = Invoice.STATUS_MANUAL_REVIEW
        else:
            reversal.status = PaymentReversal.STATUS_APPLIED
            invoice.status = (
                Invoice.STATUS_REFUNDED
                if new_refunded_amount == invoice.amount
                else Invoice.STATUS_PARTIALLY_REFUNDED
            )
        reversal.save(update_fields=[
            'credits_requested', 'credits_reversed', 'credit_shortfall',
            'status', 'reason', 'updated_at',
        ])
        invoice.save(update_fields=[
            'refunded_amount', 'refund_review_required', 'status', 'updated_at',
        ])
        return reversal

    @staticmethod
    def record_chargeback(
        invoice: Invoice,
        amount,
        *,
        external_reference: str,
        currency: str = 'RUB',
    ) -> PaymentReversal | None:
        """Внутренняя точка для ручного/реестрового учёта чарджбэка."""
        return BillingService.handle_reversal_success(
            provider_reference=external_reference,
            payment_id=invoice.yookassa_payment_id,
            amount=amount,
            currency=currency,
            kind=PaymentReversal.KIND_CHARGEBACK,
        )

    @staticmethod
    @transaction.atomic
    def start_trial(tenant: Tenant) -> Subscription:
        """Запускает 14-дневный пробный период на плане Business."""
        plan = Plan.objects.get(slug=Plan.SLUG_BUSINESS)
        today = timezone.localdate()

        trial_end = today + timedelta(days=TRIAL_DAYS)
        subscription = Subscription.objects.create(
            tenant=tenant,
            plan=plan,
            status=Subscription.STATUS_TRIAL,
            billing_period=Subscription.PERIOD_MONTHLY,
            current_period_start=today,
            current_period_end=trial_end,
            ai_period_start=today,
            ai_period_end=trial_end,
        )
        BillingService.sync_tenant_trial_end(subscription)
        from apps.billing.ai_wallet import AIWalletService
        AIWalletService.grant_included(
            tenant,
            AIWalletService.effective_limit(tenant),
            period_end=subscription.ai_period_end,
            idempotency_key=(
                f'subscription-grant:{subscription.pk}:'
                f'{subscription.ai_period_start}:{subscription.ai_period_end}'
            ),
        )
        logger.info('Trial запущен для тенанта %s, план %s', tenant.slug, plan.slug)
        return subscription

    @staticmethod
    @transaction.atomic
    def extend_trial(tenant: Tenant, days: int | None = None) -> Subscription:
        """Продлевает trial, не позволяя случайно понизить активную оплату."""
        extension_days = days if days is not None else TRIAL_DAYS
        if extension_days <= 0:
            raise ValueError('Срок продления должен быть положительным.')

        sub = Subscription.objects.select_for_update().get(tenant=tenant)
        if sub.status == Subscription.STATUS_ACTIVE:
            raise ValueError('Нельзя заменить активную платную подписку триалом.')

        today = timezone.localdate()
        base = max(sub.current_period_end, today)
        sub.status = Subscription.STATUS_TRIAL
        sub.current_period_start = today
        sub.current_period_end = base + timedelta(days=extension_days)
        sub.ai_period_start = today
        sub.ai_period_end = sub.current_period_end
        sub.cancelled_at = None
        sub.save(update_fields=[
            'status', 'current_period_start', 'current_period_end',
            'ai_period_start', 'ai_period_end', 'cancelled_at',
        ])
        BillingService.sync_tenant_trial_end(sub)
        return sub

    @staticmethod
    @transaction.atomic
    def upgrade_plan(tenant: Tenant, plan_slug: str, period: str) -> Subscription:
        """Меняет тарифный план тенанта."""
        plan = Plan.objects.get(slug=plan_slug, is_active=True)
        today = timezone.localdate()

        if period == Subscription.PERIOD_YEARLY:
            end = add_billing_year(today)
        else:
            end = add_billing_month(today)

        sub = tenant.subscription
        sub.plan = plan
        sub.billing_period = period
        sub.status = Subscription.STATUS_ACTIVE
        sub.current_period_start = today
        sub.current_period_end = end
        sub.ai_period_start = today
        sub.ai_period_end = (
            min(add_billing_months(today, 1), end)
            if period == Subscription.PERIOD_YEARLY
            else end
        )
        sub.cancelled_at = None
        sub.save()
        BillingService.sync_tenant_trial_end(sub)

        # Новый расчётный период — начисляем включённый AI-баланс.
        Tenant.objects.filter(pk=tenant.pk).update(ai_credits_used=0)
        from apps.billing.ai_wallet import AIWalletService
        AIWalletService.grant_included(
            tenant,
            AIWalletService.effective_limit(tenant),
            period_end=sub.ai_period_end,
            idempotency_key=(
                f'subscription-grant:{sub.pk}:'
                f'{sub.ai_period_start}:{sub.ai_period_end}'
            ),
        )

        logger.info('Тенант %s перешёл на план %s', tenant.slug, plan.slug)
        return sub

    @staticmethod
    @transaction.atomic
    def refresh_ai_credit_period(subscription_id: int, target_date: date | None = None) -> bool:
        """Начисляет пакет текущего AI-месяца ровно один раз."""
        sub = Subscription.objects.select_for_update().select_related(
            'tenant', 'plan',
        ).get(pk=subscription_id)
        today = target_date or timezone.localdate()
        if sub.status != Subscription.STATUS_ACTIVE:
            return False

        period = ai_credit_period_for_date(sub, today)
        if period is None:
            return False
        period_start, period_end = period
        if sub.ai_period_start == period_start and sub.ai_period_end == period_end:
            return False

        sub.ai_period_start = period_start
        sub.ai_period_end = period_end
        sub.save(update_fields=['ai_period_start', 'ai_period_end', 'updated_at'])

        Tenant.objects.filter(pk=sub.tenant_id).update(ai_credits_used=0)
        from apps.billing.ai_wallet import AIWalletService
        AIWalletService.grant_included(
            sub.tenant,
            AIWalletService.effective_limit(sub.tenant),
            period_end=period_end,
            idempotency_key=(
                f'subscription-grant:{sub.pk}:{period_start}:{period_end}'
            ),
        )
        return True

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
        BillingService.sync_tenant_trial_end(sub)

        BillingService._requeue_limit_reached_listings(tenant)
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
        Переводит просроченные trial/active-подписки в past_due.
        Вызывается Celery Beat ежедневно.
        Возвращает количество обработанных подписок.
        """
        today = timezone.localdate()
        expired = Subscription.objects.filter(
            status__in=(Subscription.STATUS_TRIAL, Subscription.STATUS_ACTIVE),
            current_period_end__lt=today,
        )
        tenant_ids = list(expired.values_list('tenant_id', flat=True))
        count = expired.update(status=Subscription.STATUS_PAST_DUE)
        if tenant_ids:
            Tenant.objects.filter(pk__in=tenant_ids).update(trial_ends_at=None)
        if count:
            logger.warning('Переведено %d истёкших подписок в past_due', count)
        return count

    @staticmethod
    def check_grace_period_expired() -> int:
        """
        Отменяет подписки, у которых истёк grace period (7 дней past_due).
        Вызывается Celery Beat ежедневно.
        """
        deadline = timezone.localdate() - timedelta(days=GRACE_PERIOD_DAYS)
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
