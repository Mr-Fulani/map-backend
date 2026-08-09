import calendar
import hashlib
import json
import logging
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from urllib.parse import urlsplit

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.billing.models import (
    AICreditPackage, AICreditTransaction, CheckoutIntentKey, Invoice,
    PaymentReversal, Plan, Subscription,
)
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

TRIAL_DAYS = settings.BILLING_TRIAL_DAYS
_CHECKOUT_NAMESPACE = uuid.UUID('73523028-9056-5d55-88b7-3cda8d825c5f')
_PROVIDER_IDEMPOTENCY_HORIZON = timedelta(hours=23)


class CheckoutConflictError(RuntimeError):
    """Client idempotency key was reused with a different checkout payload."""

    def __init__(self, invoice_id: int):
        self.invoice_id = invoice_id
        super().__init__(
            'Ключ идемпотентности уже использован для другой покупки.',
        )


class CheckoutKeyLimitError(RuntimeError):
    """An active intent already has the maximum number of browser aliases."""

    def __init__(self, invoice_id: int):
        self.invoice_id = invoice_id
        super().__init__(
            'Для checkout intent исчерпан лимит ключей; '
            'повторите запрос с ранее выданным idempotency_key.',
        )


class CheckoutPendingError(RuntimeError):
    """Provider outcome is ambiguous; retrying must reuse the same intent."""

    def __init__(self, invoice_id: int, retry_after: int):
        self.invoice_id = invoice_id
        self.retry_after = retry_after
        super().__init__(
            'Результат создания платежа уточняется; '
            'повторите запрос с тем же idempotency_key.',
        )


class CheckoutManualReviewError(RuntimeError):
    """The intent cannot be retried automatically without duplicate-charge risk."""

    def __init__(self, invoice_id: int, reason: str):
        self.invoice_id = invoice_id
        self.reason = reason
        super().__init__(reason)


class CheckoutTerminalError(RuntimeError):
    """The client key belongs to a completed intent and may be rotated."""

    def __init__(self, invoice_id: int, invoice_status: str):
        self.invoice_id = invoice_id
        self.invoice_status = invoice_status
        super().__init__(
            f'Checkout intent уже завершён со статусом {invoice_status}.',
        )


def _checkout_payload_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _provider_checkout_key(tenant_id: int, client_key: uuid.UUID) -> str:
    return str(uuid.uuid5(
        _CHECKOUT_NAMESPACE,
        f'yookassa:tenant:{tenant_id}:checkout:{client_key}',
    ))


def _assert_checkout_transaction_boundary() -> None:
    """Provider checkout must never run inside an application transaction.

    The durable Invoice intent has to commit before the network request starts.
    Django's TestCase wrapper is intentionally ignored so unit tests retain
    their normal rollback isolation; an explicit nested ``atomic()`` is still
    detected by the regression test.
    """
    connection = transaction.get_connection()
    application_atomic_blocks = [
        block
        for block in getattr(connection, 'atomic_blocks', ())
        if not getattr(block, '_from_testcase', False)
    ]
    manual_transaction = (
        not connection.get_autocommit()
        and not connection.in_atomic_block
    )
    if application_atomic_blocks or manual_transaction:
        raise RuntimeError(
            'Checkout нельзя запускать внутри внешней DB-транзакции: '
            'Invoice intent должен быть зафиксирован до вызова YooKassa.',
        )


def reconciliation_delay_seconds(attempt: int) -> int:
    exponent = min(max(0, attempt - 1), 16)
    return min(
        settings.YOOKASSA_RECONCILIATION_MAX_DELAY_SECONDS,
        settings.YOOKASSA_RECONCILIATION_BASE_DELAY_SECONDS * (2 ** exponent),
    )


def _is_safe_confirmation_url(value: str) -> bool:
    try:
        parsed = urlsplit(str(value or ''))
        parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == 'https'
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    )


def _write_billing_log(tenant, status: str, message: str) -> None:
    """Записывает биллинговое событие в SyncLog — не падает при ошибках."""
    try:
        from apps.sync.models import SyncLog
        # A nested savepoint prevents a failed best-effort audit insert from
        # poisoning an outer financial transaction after the exception is caught.
        with transaction.atomic():
            SyncLog.objects.create(
                tenant=tenant,
                event_type=SyncLog.EVENT_BILLING,
                status=status,
                message=message,
            )
    except Exception:
        logger.exception('Не удалось записать billing audit log для tenant=%s.', tenant.pk)


def _reversal_matches(
    reversal: PaymentReversal,
    *,
    invoice: Invoice,
    provider_reference: str,
    payment_id: str,
    amount: Decimal,
    currency: str,
    kind: str,
) -> bool:
    return (
        reversal.provider_reference == provider_reference
        and reversal.invoice_id == invoice.pk
        and reversal.payment_id == payment_id
        and reversal.amount == amount
        and reversal.currency == currency
        and reversal.kind == kind
    )


def _create_reversal_fail_closed(
    *,
    invoice: Invoice,
    provider_reference: str,
    payment_id: str,
    amount: Decimal,
    currency: str,
    kind: str,
    status: str,
    reason: str = '',
) -> tuple[PaymentReversal | None, bool]:
    """Создаёт reversal в savepoint и безопасно разрешает global unique race."""
    try:
        with transaction.atomic():
            reversal = PaymentReversal.objects.create(
                invoice=invoice,
                kind=kind,
                provider_reference=provider_reference,
                payment_id=payment_id,
                amount=amount,
                currency=currency,
                status=status,
                reason=reason,
            )
    except IntegrityError:
        try:
            existing = PaymentReversal.objects.get(
                provider_reference=provider_reference,
            )
        except PaymentReversal.DoesNotExist:
            logger.exception(
                'Коллизия provider_reference=%s без доступной строки reversal.',
                provider_reference,
            )
            return None, False
        if _reversal_matches(
            existing,
            invoice=invoice,
            provider_reference=provider_reference,
            payment_id=payment_id,
            amount=amount,
            currency=currency,
            kind=kind,
        ):
            return existing, False
        logger.error(
            'provider_reference=%s уже принадлежит другому возврату.',
            provider_reference,
        )
        return None, False
    return reversal, True


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
    def _normalize_client_checkout_key(value) -> uuid.UUID:
        try:
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        except (AttributeError, TypeError, ValueError):
            raise ValueError('idempotency_key должен быть UUID.') from None

    @staticmethod
    def _bind_checkout_client_key(
        *,
        tenant_id: int,
        invoice: Invoice,
        client_key: uuid.UUID,
        payload_hash: str,
    ) -> None:
        """Durably binds canonical and coalesced browser keys to one intent."""
        with transaction.atomic():
            locked_invoice = Invoice.objects.select_for_update().only(
                'pk', 'checkout_client_key',
            ).get(
                pk=invoice.pk,
                tenant_id=tenant_id,
            )
            canonical = Invoice.objects.filter(
                tenant_id=tenant_id,
                checkout_client_key=client_key,
            ).only('pk').first()
            if canonical is not None and canonical.pk != locked_invoice.pk:
                raise CheckoutConflictError(canonical.pk)

            key_record = CheckoutIntentKey.objects.filter(
                tenant_id=tenant_id,
                client_key=client_key,
            ).first()
            if key_record is None:
                alias_count = CheckoutIntentKey.objects.filter(
                    invoice_id=locked_invoice.pk,
                ).count()
                is_canonical_key = (
                    locked_invoice.checkout_client_key == client_key
                )
                canonical_key_is_reserved = (
                    locked_invoice.checkout_client_key is not None
                    and not CheckoutIntentKey.objects.filter(
                        tenant_id=tenant_id,
                        invoice_id=locked_invoice.pk,
                        client_key=locked_invoice.checkout_client_key,
                    ).exists()
                )
                accepted_key_count = alias_count + int(
                    canonical_key_is_reserved,
                )
                if (
                    not is_canonical_key
                    and accepted_key_count
                    >= settings.BILLING_CHECKOUT_MAX_KEYS_PER_INVOICE
                ):
                    raise CheckoutKeyLimitError(locked_invoice.pk)
                key_record, _ = CheckoutIntentKey.objects.get_or_create(
                    tenant_id=tenant_id,
                    client_key=client_key,
                    defaults={
                        'invoice_id': locked_invoice.pk,
                        'checkout_payload_hash': payload_hash,
                    },
                )
            if (
                key_record.invoice_id != locked_invoice.pk
                or key_record.checkout_payload_hash != payload_hash
            ):
                raise CheckoutConflictError(key_record.invoice_id)

    @staticmethod
    def _checkout_intent_for_client_key(
        *,
        tenant_id: int,
        client_key: uuid.UUID,
        payload_hash: str,
        bind_legacy_key: bool = True,
    ) -> Invoice | None:
        key_record = (
            CheckoutIntentKey.objects.select_related('invoice')
            .filter(tenant_id=tenant_id, client_key=client_key)
            .first()
        )
        if key_record is not None:
            if (
                key_record.checkout_payload_hash != payload_hash
                or key_record.invoice.checkout_payload_hash != payload_hash
            ):
                raise CheckoutConflictError(key_record.invoice_id)
            return key_record.invoice

        # Compatibility path for canonical keys created before the registry
        # migration or by an interrupted rolling deployment.
        invoice = Invoice.objects.filter(
            tenant_id=tenant_id,
            checkout_client_key=client_key,
        ).first()
        if invoice is None:
            return None
        if invoice.checkout_payload_hash != payload_hash:
            raise CheckoutConflictError(invoice.pk)
        if bind_legacy_key:
            BillingService._bind_checkout_client_key(
                tenant_id=tenant_id,
                invoice=invoice,
                client_key=client_key,
                payload_hash=payload_hash,
            )
        return invoice

    @staticmethod
    def _subscription_checkout_intent(
        *,
        tenant: Tenant,
        plan_slug: str,
        period: str,
        return_url: str,
        client_key: uuid.UUID,
        payload_hash: str,
    ) -> Invoice:
        # Immutable intents can be returned without locking their Invoice. For a
        # new key, Subscription is the per-tenant creation mutex; any FK binding
        # to an existing Invoice happens only after that mutex is released.
        existing = BillingService._checkout_intent_for_client_key(
            tenant_id=tenant.pk,
            client_key=client_key,
            payload_hash=payload_hash,
        )
        if existing is not None:
            return existing

        with transaction.atomic():
            subscription = Subscription.objects.select_for_update().get(
                tenant_id=tenant.pk,
            )
            invoice = BillingService._checkout_intent_for_client_key(
                tenant_id=tenant.pk,
                client_key=client_key,
                payload_hash=payload_hash,
                bind_legacy_key=False,
            )
            if invoice is None:
                # Different browser tabs may legitimately generate different
                # UUIDs for the same purchase. The Subscription lock serializes
                # intent creation; the partial constraint is the final invariant.
                invoice = Invoice.objects.filter(
                    tenant_id=tenant.pk,
                    purchase_type=Invoice.TYPE_SUBSCRIPTION,
                    checkout_payload_hash=payload_hash,
                    status=Invoice.STATUS_PENDING,
                    checkout_state__in=(
                        Invoice.CHECKOUT_INTENT_CREATED,
                        Invoice.CHECKOUT_PROVIDER_PENDING,
                        Invoice.CHECKOUT_PROVIDER_CREATED,
                    ),
                ).order_by('created_at', 'pk').first()

            if invoice is None:
                plan = Plan.objects.get(slug=plan_slug, is_active=True)
                amount = (
                    plan.price_yearly
                    if period == Subscription.PERIOD_YEARLY
                    else plan.price_monthly
                )
                snapshot = {
                    'schema': 1,
                    'purchase_type': Invoice.TYPE_SUBSCRIPTION,
                    'amount': str(amount),
                    'currency': 'RUB',
                    'period': period,
                    'expected_subscription_version': subscription.billing_version,
                    'plan': {
                        'id': plan.pk,
                        'slug': plan.slug,
                        'name': plan.name,
                    },
                }
                invoice = Invoice.objects.create(
                    tenant_id=tenant.pk,
                    amount=amount,
                    currency='RUB',
                    status=Invoice.STATUS_PENDING,
                    purchase_type=Invoice.TYPE_SUBSCRIPTION,
                    metadata={'plan_slug': plan.slug, 'period': period},
                    checkout_client_key=client_key,
                    provider_idempotency_key=_provider_checkout_key(
                        tenant.pk,
                        client_key,
                    ),
                    checkout_payload_hash=payload_hash,
                    checkout_return_url=return_url,
                    checkout_state=Invoice.CHECKOUT_INTENT_CREATED,
                    entitlement_snapshot=snapshot,
                    entitlement_plan=plan,
                    expected_subscription_version=subscription.billing_version,
                )

        # CheckoutIntentKey has an FK to Invoice. Bind only after releasing the
        # Subscription mutex so payment fulfillment (Invoice -> Subscription)
        # cannot deadlock with a coalesced browser tab (Subscription -> Invoice).
        BillingService._bind_checkout_client_key(
            tenant_id=tenant.pk,
            invoice=invoice,
            client_key=client_key,
            payload_hash=payload_hash,
        )
        return invoice

    @staticmethod
    def _ai_checkout_intent(
        *,
        tenant: Tenant,
        package_id: int,
        return_url: str,
        client_key: uuid.UUID,
        payload_hash: str,
    ) -> Invoice:
        existing = BillingService._checkout_intent_for_client_key(
            tenant_id=tenant.pk,
            client_key=client_key,
            payload_hash=payload_hash,
        )
        if existing is not None:
            return existing

        with transaction.atomic():
            Subscription.objects.select_for_update().only('pk').get(
                tenant_id=tenant.pk,
            )
            invoice = BillingService._checkout_intent_for_client_key(
                tenant_id=tenant.pk,
                client_key=client_key,
                payload_hash=payload_hash,
                bind_legacy_key=False,
            )
            if invoice is None:
                invoice = Invoice.objects.filter(
                    tenant_id=tenant.pk,
                    purchase_type=Invoice.TYPE_AI_TOPUP,
                    checkout_payload_hash=payload_hash,
                    status=Invoice.STATUS_PENDING,
                    checkout_state__in=(
                        Invoice.CHECKOUT_INTENT_CREATED,
                        Invoice.CHECKOUT_PROVIDER_PENDING,
                        Invoice.CHECKOUT_PROVIDER_CREATED,
                    ),
                ).order_by('created_at', 'pk').first()

            if invoice is None:
                package = AICreditPackage.objects.get(pk=package_id, is_active=True)
                metadata = {
                    'tenant_id': str(tenant.pk),
                    'purchase_type': Invoice.TYPE_AI_TOPUP,
                    'package_id': str(package.pk),
                }
                snapshot = {
                    'schema': 1,
                    'purchase_type': Invoice.TYPE_AI_TOPUP,
                    'amount': str(package.price_rub),
                    'currency': 'RUB',
                    'package': {
                        'id': package.pk,
                        'name': package.name,
                        'credits': str(package.credits),
                        'price_rub': str(package.price_rub),
                    },
                }
                invoice = Invoice.objects.create(
                    tenant_id=tenant.pk,
                    amount=package.price_rub,
                    currency='RUB',
                    status=Invoice.STATUS_PENDING,
                    purchase_type=Invoice.TYPE_AI_TOPUP,
                    metadata=metadata,
                    checkout_client_key=client_key,
                    provider_idempotency_key=_provider_checkout_key(
                        tenant.pk,
                        client_key,
                    ),
                    checkout_payload_hash=payload_hash,
                    checkout_return_url=return_url,
                    checkout_state=Invoice.CHECKOUT_INTENT_CREATED,
                    entitlement_snapshot=snapshot,
                )

        BillingService._bind_checkout_client_key(
            tenant_id=tenant.pk,
            invoice=invoice,
            client_key=client_key,
            payload_hash=payload_hash,
        )
        return invoice

    @staticmethod
    def _provider_payment_parameters(invoice: Invoice) -> tuple[str, dict]:
        snapshot = invoice.entitlement_snapshot or {}
        if invoice.purchase_type == Invoice.TYPE_AI_TOPUP:
            package = snapshot.get('package') or {}
            package_name = str(package.get('name') or '').strip()
            if not package_name:
                raise CheckoutManualReviewError(
                    invoice.pk,
                    'В checkout intent отсутствует snapshot AI-пакета.',
                )
            metadata = {
                'tenant_id': str(invoice.tenant_id),
                'purchase_type': Invoice.TYPE_AI_TOPUP,
                'package_id': str(package.get('id')),
                'checkout_intent_id': str(invoice.pk),
                'checkout_client_key': str(invoice.checkout_client_key),
            }
            return f'MAP — {package_name}'[:128], metadata

        plan = snapshot.get('plan') or {}
        plan_name = str(plan.get('name') or '').strip()
        period = snapshot.get('period')
        if not plan_name or period not in {
            Subscription.PERIOD_MONTHLY,
            Subscription.PERIOD_YEARLY,
        }:
            raise CheckoutManualReviewError(
                invoice.pk,
                'В checkout intent отсутствует snapshot тарифа.',
            )
        period_label = 'год' if period == Subscription.PERIOD_YEARLY else 'месяц'
        metadata = {
            'tenant_id': str(invoice.tenant_id),
            'plan_slug': str(plan.get('slug')),
            'period': period,
            'checkout_intent_id': str(invoice.pk),
            'checkout_client_key': str(invoice.checkout_client_key),
        }
        return f'MAP {plan_name} ({period_label})'[:128], metadata

    @staticmethod
    def _set_checkout_manual_review(invoice_id: int, reason: str) -> None:
        with transaction.atomic():
            invoice = Invoice.objects.select_for_update().select_related('tenant').get(
                pk=invoice_id,
            )
            invoice.status = Invoice.STATUS_MANUAL_REVIEW
            invoice.checkout_state = Invoice.CHECKOUT_MANUAL_REVIEW
            invoice.checkout_last_error = reason[:500]
            invoice.next_reconciliation_at = None
            invoice.save(update_fields=[
                'status', 'checkout_state', 'checkout_last_error',
                'next_reconciliation_at', 'updated_at',
            ])
            tenant = invoice.tenant
        logger.error('Invoice %s переведён на ручную проверку: %s', invoice_id, reason)
        _write_billing_log(tenant, 'error', f'Invoice {invoice_id}: {reason}')

    @staticmethod
    @transaction.atomic
    def _mark_payment_collision_manual(
        invoice_id: int,
        payment_id: str,
        reason: str,
    ) -> None:
        invoices = list(
            Invoice.objects.select_for_update()
            .select_related('tenant')
            .filter(Q(pk=invoice_id) | Q(yookassa_payment_id=payment_id))
            .order_by('pk')
        )
        for invoice in invoices:
            BillingService._mark_invoice_manual_review_locked(invoice, reason)

    @staticmethod
    def _resume_checkout_intent(
        invoice_id: int,
        *,
        respect_backoff: bool = False,
    ) -> str:
        _assert_checkout_transaction_boundary()
        from apps.billing.yookassa_client import (
            create_payment as yk_create,
            is_valid_provider_id,
        )

        now = timezone.now()
        manual_reason = ''
        with transaction.atomic():
            invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
            if (
                invoice.checkout_state == Invoice.CHECKOUT_MANUAL_REVIEW
                or invoice.status == Invoice.STATUS_MANUAL_REVIEW
            ):
                raise CheckoutManualReviewError(
                    invoice.pk,
                    invoice.checkout_last_error or 'Invoice требует ручной проверки.',
                )
            if invoice.status != Invoice.STATUS_PENDING:
                # A persisted provider URL is not proof that this checkout is
                # still usable. The webhook/reconciler may already have moved
                # the Invoice to a terminal state while a browser tab retained
                # its client key. Only this explicit signal authorizes the
                # client to rotate that key for a genuinely new purchase.
                raise CheckoutTerminalError(
                    invoice.pk,
                    invoice.status,
                )
            if invoice.checkout_confirmation_url:
                return invoice.checkout_confirmation_url
            if not invoice.provider_idempotency_key or not invoice.checkout_client_key:
                manual_reason = 'Checkout intent не содержит устойчивый provider key.'
            elif invoice.checkout_attempt_count >= settings.YOOKASSA_RECONCILIATION_MAX_ATTEMPTS:
                manual_reason = 'Исчерпан лимит повторов создания платежа.'
            elif (
                invoice.checkout_first_attempt_at is not None
                and invoice.checkout_first_attempt_at < now - _PROVIDER_IDEMPOTENCY_HORIZON
            ):
                manual_reason = (
                    'Истёк безопасный срок provider idempotency; '
                    'автоповтор может создать дубль платежа.'
                )
            elif (
                respect_backoff
                and invoice.next_reconciliation_at is not None
                and invoice.next_reconciliation_at > now
            ):
                retry_after = max(
                    1,
                    int((invoice.next_reconciliation_at - now).total_seconds()),
                )
                raise CheckoutPendingError(invoice.pk, retry_after)

            if manual_reason:
                invoice.status = Invoice.STATUS_MANUAL_REVIEW
                invoice.checkout_state = Invoice.CHECKOUT_MANUAL_REVIEW
                invoice.checkout_last_error = manual_reason
                invoice.next_reconciliation_at = None
                invoice.save(update_fields=[
                    'status', 'checkout_state', 'checkout_last_error',
                    'next_reconciliation_at', 'updated_at',
                ])
                manual_invoice_id = invoice.pk
            else:
                invoice.checkout_attempt_count += 1
                if invoice.checkout_first_attempt_at is None:
                    invoice.checkout_first_attempt_at = now
                invoice.checkout_last_attempt_at = now
                invoice.checkout_state = Invoice.CHECKOUT_PROVIDER_PENDING
                attempt_count = invoice.checkout_attempt_count
                retry_after = reconciliation_delay_seconds(attempt_count)
                invoice.next_reconciliation_at = now + timedelta(seconds=retry_after)
                try:
                    description, provider_metadata = (
                        BillingService._provider_payment_parameters(invoice)
                    )
                except CheckoutManualReviewError as exc:
                    manual_reason = exc.reason
                    invoice.status = Invoice.STATUS_MANUAL_REVIEW
                    invoice.checkout_state = Invoice.CHECKOUT_MANUAL_REVIEW
                    invoice.checkout_last_error = manual_reason
                    invoice.next_reconciliation_at = None
                invoice.save(update_fields=[
                    'checkout_attempt_count', 'checkout_first_attempt_at',
                    'checkout_last_attempt_at', 'checkout_state',
                    'checkout_last_error', 'status', 'next_reconciliation_at',
                    'updated_at',
                ])
                manual_invoice_id = invoice.pk if manual_reason else None
                if not manual_reason:
                    provider_key = invoice.provider_idempotency_key
                    amount = invoice.amount
                    return_url = invoice.checkout_return_url

        if manual_invoice_id is not None:
            BillingService._set_checkout_manual_review(manual_invoice_id, manual_reason)
            raise CheckoutManualReviewError(manual_invoice_id, manual_reason)

        try:
            payment_id, confirmation_url = yk_create(
                amount=amount,
                description=description,
                return_url=return_url,
                metadata=provider_metadata,
                idempotency_key=provider_key,
            )
            if not is_valid_provider_id(payment_id) or not _is_safe_confirmation_url(
                confirmation_url,
            ):
                raise ValueError('YooKassa вернула некорректный payment response.')
        except CheckoutManualReviewError:
            raise
        except Exception as exc:
            retry_after = reconciliation_delay_seconds(attempt_count)
            with transaction.atomic():
                invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
                if not invoice.checkout_confirmation_url:
                    invoice.checkout_state = Invoice.CHECKOUT_PROVIDER_PENDING
                    invoice.checkout_last_error = (
                        f'Неопределённый результат YooKassa: {type(exc).__name__}'
                    )[:500]
                    invoice.next_reconciliation_at = timezone.now() + timedelta(
                        seconds=retry_after,
                    )
                    invoice.save(update_fields=[
                        'checkout_state', 'checkout_last_error',
                        'next_reconciliation_at', 'updated_at',
                    ])
            logger.warning(
                'Неопределённый результат YooKassa для invoice=%s.',
                invoice_id,
                exc_info=True,
            )
            raise CheckoutPendingError(invoice_id, retry_after) from exc

        try:
            with transaction.atomic():
                invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
                if (
                    invoice.yookassa_payment_id
                    and invoice.yookassa_payment_id != payment_id
                ):
                    raise CheckoutManualReviewError(
                        invoice.pk,
                        'Provider key вернул другой payment_id.',
                    )
                invoice.yookassa_payment_id = payment_id
                invoice.checkout_confirmation_url = confirmation_url
                invoice.checkout_state = Invoice.CHECKOUT_PROVIDER_CREATED
                invoice.checkout_last_error = ''
                invoice.next_reconciliation_at = timezone.now() + timedelta(
                    seconds=settings.YOOKASSA_RECONCILIATION_BASE_DELAY_SECONDS,
                )
                invoice.save(update_fields=[
                    'yookassa_payment_id', 'checkout_confirmation_url',
                    'checkout_state', 'checkout_last_error',
                    'next_reconciliation_at', 'updated_at',
                ])
        except CheckoutManualReviewError as exc:
            BillingService._mark_payment_collision_manual(
                invoice_id,
                payment_id,
                exc.reason,
            )
            raise
        except IntegrityError as exc:
            reason = 'payment_id уже связан с другим Invoice; оба счёта заблокированы.'
            BillingService._mark_payment_collision_manual(
                invoice_id,
                payment_id,
                reason,
            )
            raise CheckoutManualReviewError(invoice_id, reason) from exc
        except Exception as exc:
            retry_after = reconciliation_delay_seconds(attempt_count)
            logger.exception(
                'Платёж YooKassa создан, но не удалось '
                'сохранить provider response: invoice=%s.',
                invoice_id,
            )
            raise CheckoutPendingError(invoice_id, retry_after) from exc
        return confirmation_url

    @staticmethod
    def create_payment(
        tenant: Tenant,
        plan_slug: str,
        period: str,
        return_url: str,
        *,
        idempotency_key,
    ) -> str:
        """Creates or safely resumes one durable subscription checkout intent."""
        _assert_checkout_transaction_boundary()
        client_key = BillingService._normalize_client_checkout_key(idempotency_key)
        payload_hash = _checkout_payload_hash({
            'schema': 1,
            'purchase_type': Invoice.TYPE_SUBSCRIPTION,
            'plan_slug': plan_slug,
            'period': period,
            'return_url': return_url,
        })
        invoice = BillingService._subscription_checkout_intent(
            tenant=tenant,
            plan_slug=plan_slug,
            period=period,
            return_url=return_url,
            client_key=client_key,
            payload_hash=payload_hash,
        )
        return BillingService._resume_checkout_intent(
            invoice.pk,
            respect_backoff=True,
        )

    @staticmethod
    def create_ai_topup_payment(
        tenant: Tenant,
        package_id: int,
        return_url: str,
        *,
        idempotency_key,
    ) -> str:
        """Creates or safely resumes one durable AI top-up checkout intent."""
        _assert_checkout_transaction_boundary()
        client_key = BillingService._normalize_client_checkout_key(idempotency_key)
        payload_hash = _checkout_payload_hash({
            'schema': 1,
            'purchase_type': Invoice.TYPE_AI_TOPUP,
            'package_id': package_id,
            'return_url': return_url,
        })
        invoice = BillingService._ai_checkout_intent(
            tenant=tenant,
            package_id=package_id,
            return_url=return_url,
            client_key=client_key,
            payload_hash=payload_hash,
        )
        return BillingService._resume_checkout_intent(
            invoice.pk,
            respect_backoff=True,
        )

    @staticmethod
    def _mark_invoice_manual_review_locked(
        invoice: Invoice,
        reason: str,
        *,
        refund_review: bool = False,
    ) -> None:
        invoice.status = Invoice.STATUS_MANUAL_REVIEW
        if invoice.checkout_client_key is not None:
            invoice.checkout_state = Invoice.CHECKOUT_MANUAL_REVIEW
        invoice.checkout_last_error = reason[:500]
        invoice.next_reconciliation_at = None
        update_fields = [
            'status', 'checkout_state', 'checkout_last_error',
            'next_reconciliation_at', 'updated_at',
        ]
        if refund_review:
            invoice.refund_review_required = True
            update_fields.append('refund_review_required')
        invoice.save(update_fields=update_fields)
        logger.error('Invoice %s требует ручной проверки: %s', invoice.pk, reason)
        _write_billing_log(invoice.tenant, 'error', f'Invoice {invoice.pk}: {reason}')

    @staticmethod
    @transaction.atomic
    def mark_invoice_manual_review(
        invoice_id: int,
        reason: str,
        *,
        refund_review: bool = False,
    ) -> Invoice:
        invoice = Invoice.objects.select_for_update().select_related('tenant').get(
            pk=invoice_id,
        )
        BillingService._mark_invoice_manual_review_locked(
            invoice,
            reason,
            refund_review=refund_review,
        )
        return invoice

    @staticmethod
    def _ai_entitlement_from_invoice(invoice: Invoice) -> tuple[Decimal | None, dict]:
        snapshot = invoice.entitlement_snapshot or {}
        if snapshot:
            package_snapshot = snapshot.get('package') or {}
            try:
                credits = Decimal(str(package_snapshot.get('credits')))
                snapshot_amount = Decimal(str(snapshot.get('amount')))
                package_price = Decimal(str(package_snapshot.get('price_rub')))
            except (InvalidOperation, TypeError, ValueError):
                return None, snapshot
            if (
                snapshot.get('schema') != 1
                or snapshot.get('purchase_type') != Invoice.TYPE_AI_TOPUP
                or snapshot.get('currency') != invoice.currency
                or not credits.is_finite()
                or credits <= 0
                or not snapshot_amount.is_finite()
                or snapshot_amount != invoice.amount
                or not package_price.is_finite()
                or package_price != invoice.amount
            ):
                return None, snapshot
            return credits, snapshot

        # Backward compatibility: for an already fulfilled legacy invoice, the
        # immutable ledger contains the exact amount that was originally granted.
        transaction_rows = list(AICreditTransaction.objects.filter(
            tenant_id=invoice.tenant_id,
            kind=AICreditTransaction.KIND_TOPUP,
        ).filter(
            Q(idempotency_key=f'yookassa-topup:{invoice.yookassa_payment_id}')
            | Q(reference=invoice.yookassa_payment_id),
        ).order_by('created_at')[:2])
        if len(transaction_rows) == 1 and transaction_rows[0].amount > 0:
            transaction_row = transaction_rows[0]
            ledger_snapshot = {
                'schema': 1,
                'purchase_type': Invoice.TYPE_AI_TOPUP,
                'amount': str(invoice.amount),
                'currency': invoice.currency,
                'package': {
                    'id': (invoice.metadata or {}).get('package_id'),
                    'name': 'Legacy AI top-up',
                    'credits': str(transaction_row.amount),
                    'price_rub': str(invoice.amount),
                },
                'legacy_ledger_backfill': True,
            }
            return transaction_row.amount, ledger_snapshot
        return None, snapshot

    @staticmethod
    @transaction.atomic
    def handle_payment_success_webhook(
        invoice_id: int,
        *,
        payment_id: str,
        amount,
        currency: str,
    ) -> bool:
        """
        Обрабатывает вебхук payment.succeeded от YooKassa.

        Обновляет существующий Invoice до статуса paid и активирует подписку.
        Отправляет email-уведомление тенанту.
        """
        from apps.billing.outbox import enqueue_notification

        try:
            invoice = Invoice.objects.select_for_update().select_related('tenant').get(
                pk=invoice_id,
            )
        except Invoice.DoesNotExist:
            logger.warning('handle_payment_success_webhook: Invoice %s не найден', invoice_id)
            return False

        if not payment_id or invoice.yookassa_payment_id != payment_id:
            BillingService._mark_invoice_manual_review_locked(
                invoice,
                f'Invoice не принадлежит payment_id={payment_id}.',
            )
            return False

        try:
            raw_amount = Decimal(str(amount))
            received_amount = raw_amount.quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError, ValueError):
            BillingService._mark_invoice_manual_review_locked(
                invoice,
                'Авторитетный платёж содержит некорректную сумму.',
            )
            return False
        if (
            not raw_amount.is_finite()
            or raw_amount <= 0
            or raw_amount != received_amount
        ):
            BillingService._mark_invoice_manual_review_locked(
                invoice,
                'Авторитетный платёж содержит некорректную сумму.',
            )
            return False

        received_currency = str(currency or '').upper()
        if (
            len(received_currency) != 3
            or not received_currency.isalpha()
            or received_amount != invoice.amount
            or received_currency != invoice.currency
        ):
            BillingService._mark_invoice_manual_review_locked(
                invoice,
                (
                    f'Платёж {payment_id}: ожидалось '
                    f'{invoice.amount} {invoice.currency}, получено '
                    f'{received_amount} {received_currency or "<empty>"}.'
                ),
            )
            return False

        # Повтор после сбоя между бизнес-транзакцией и записью аудита не должен
        # продлевать подписку или уведомлять второй раз, но обязан повторно
        # подтвердить идентификатор, сумму и валюту.
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

        tenant = invoice.tenant
        stored_metadata = invoice.metadata or {}
        if invoice.purchase_type == Invoice.TYPE_AI_TOPUP:
            credits, snapshot = BillingService._ai_entitlement_from_invoice(invoice)
            if credits is None:
                BillingService._mark_invoice_manual_review_locked(
                    invoice,
                    'Нельзя определить купленное число AI-кредитов.',
                )
                return False
            if not invoice.entitlement_snapshot:
                invoice.entitlement_snapshot = snapshot
                invoice.save(update_fields=['entitlement_snapshot', 'updated_at'])

            from apps.billing.ai_wallet import AIWalletService
            invoice.status = Invoice.STATUS_PAID
            invoice.paid_at = timezone.now()
            invoice.checkout_last_error = ''
            invoice.next_reconciliation_at = None
            invoice.save(update_fields=[
                'status', 'paid_at', 'checkout_last_error',
                'next_reconciliation_at', 'updated_at',
            ])
            AIWalletService.topup(
                tenant,
                credits,
                idempotency_key=f'yookassa-topup:{payment_id}',
                reference=payment_id,
            )
            notification_message = (
                f'AI-баланс пополнен на {credits} кредитов.'
            )
            enqueue_notification(
                tenant=tenant,
                invoice=invoice,
                level='billing',
                message=notification_message,
                idempotency_key=f'invoice:{invoice.pk}:paid:notification:v1',
            )
            _write_billing_log(
                tenant, 'ok',
                f'AI-баланс пополнен на {credits} кредитов',
            )
            return True

        # Different invoices for one tenant lock the same Subscription row. The
        # expected version turns a concurrently paid stale intent into an
        # explicit manual-review case instead of a last-write-wins entitlement.
        try:
            sub = Subscription.objects.select_for_update().get(tenant_id=tenant.pk)
        except Subscription.DoesNotExist:
            BillingService._mark_invoice_manual_review_locked(
                invoice,
                'Подписка тенанта не найдена.',
            )
            return False

        snapshot = invoice.entitlement_snapshot or {}
        plan_snapshot = snapshot.get('plan') or {}
        legacy_status_only = bool(
            not snapshot
            and invoice.entitlement_plan_id is None
            and not stored_metadata.get('plan_slug')
        )
        period = snapshot.get('period') or stored_metadata.get(
            'period',
            Subscription.PERIOD_MONTHLY,
        )
        if snapshot:
            try:
                snapshot_amount = Decimal(str(snapshot.get('amount')))
                snapshot_plan_id = int(plan_snapshot.get('id'))
            except (InvalidOperation, TypeError, ValueError):
                BillingService._mark_invoice_manual_review_locked(
                    invoice,
                    'Неизменяемый snapshot подписки повреждён.',
                )
                return False
            if (
                snapshot.get('schema') != 1
                or snapshot.get('purchase_type') != Invoice.TYPE_SUBSCRIPTION
                or snapshot.get('currency') != invoice.currency
                or not snapshot_amount.is_finite()
                or snapshot_amount != invoice.amount
                or (
                    invoice.entitlement_plan_id is not None
                    and invoice.entitlement_plan_id != snapshot_plan_id
                )
                or snapshot.get('expected_subscription_version')
                != invoice.expected_subscription_version
            ):
                BillingService._mark_invoice_manual_review_locked(
                    invoice,
                    'Неизменяемый snapshot подписки не совпадает с Invoice.',
                )
                return False
        plan_id = invoice.entitlement_plan_id or plan_snapshot.get('id')
        plan_slug = plan_snapshot.get('slug') or stored_metadata.get('plan_slug')
        plan = None
        if plan_id:
            plan = Plan.objects.filter(pk=plan_id).first()
        elif plan_slug:
            plan = Plan.objects.filter(slug=plan_slug).first()
        else:
            # Legacy invoices without purchase metadata only activated the
            # existing plan. Preserve that behavior and freeze it now.
            plan = sub.plan

        if plan is None or period not in {
            Subscription.PERIOD_MONTHLY,
            Subscription.PERIOD_YEARLY,
        }:
            BillingService._mark_invoice_manual_review_locked(
                invoice,
                'Нельзя однозначно определить купленный тариф и период.',
            )
            return False
        if snapshot and (
            plan.pk != snapshot_plan_id
            or plan.slug != str(plan_snapshot.get('slug') or '')
        ):
            BillingService._mark_invoice_manual_review_locked(
                invoice,
                'Купленный тариф не совпадает с неизменяемым snapshot.',
            )
            return False
        if (
            invoice.expected_subscription_version is not None
            and sub.billing_version != invoice.expected_subscription_version
        ):
            BillingService._mark_invoice_manual_review_locked(
                invoice,
                (
                    'Подписка изменилась после checkout: '
                    f'expected_version={invoice.expected_subscription_version}, '
                    f'actual_version={sub.billing_version}.'
                ),
            )
            return False

        if not invoice.entitlement_snapshot:
            snapshot_period = sub.billing_period if legacy_status_only else period
            invoice.entitlement_snapshot = {
                'schema': 1,
                'purchase_type': Invoice.TYPE_SUBSCRIPTION,
                'amount': str(invoice.amount),
                'currency': invoice.currency,
                'period': snapshot_period,
                'expected_subscription_version': None,
                'plan': {'id': plan.pk, 'slug': plan.slug, 'name': plan.name},
                'legacy_backfill': True,
                'legacy_status_only': legacy_status_only,
            }
            invoice.entitlement_plan = plan

        if legacy_status_only:
            # Exact compatibility with pre-intent invoices: these payments only
            # reactivated the existing subscription and never rewrote its term.
            sub.status = Subscription.STATUS_ACTIVE
            sub.billing_version += 1
            sub.save(update_fields=['status', 'billing_version', 'updated_at'])
        else:
            today = timezone.localdate()
            end = (
                add_billing_year(today)
                if period == Subscription.PERIOD_YEARLY
                else add_billing_month(today)
            )
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
            sub.billing_version += 1
            sub.save()
        BillingService.sync_tenant_trial_end(sub)

        invoice.status = Invoice.STATUS_PAID
        invoice.paid_at = timezone.now()
        invoice.checkout_last_error = ''
        invoice.next_reconciliation_at = None
        invoice.save(update_fields=[
            'status', 'paid_at', 'entitlement_snapshot', 'entitlement_plan',
            'checkout_last_error', 'next_reconciliation_at', 'updated_at',
        ])

        if not legacy_status_only:
            Tenant.objects.filter(pk=tenant.pk).update(ai_credits_used=0)
            from apps.billing.ai_wallet import AIWalletService
            AIWalletService.grant_included(
                tenant,
                AIWalletService.effective_limit(tenant),
                period_end=sub.ai_period_end,
                idempotency_key=f'subscription-grant:{sub.pk}:v{sub.billing_version}',
            )

        notification_message = (
            f'Оплата {invoice.amount}₽ прошла успешно. Подписка активирована.'
        )
        enqueue_notification(
            tenant=tenant,
            invoice=invoice,
            level='billing',
            message=notification_message,
            idempotency_key=f'invoice:{invoice.pk}:paid:notification:v1',
        )
        BillingService._requeue_limit_reached_listings(
            tenant,
            invoice=invoice,
            idempotency_key=f'invoice:{invoice.pk}:paid:requeue:v1',
        )
        _write_billing_log(tenant, 'ok', f'Оплата {invoice.amount}₽ прошла успешно')
        logger.info('Вебхук payment.succeeded: tenant=%s, amount=%s', tenant.slug, amount)
        return True

    @staticmethod
    def _requeue_limit_reached_listings(
        tenant: Tenant,
        *,
        idempotency_key: str,
        invoice: Invoice | None = None,
    ) -> None:
        """Сохраняет durable-команду повторной публикации лимитных листингов.

        Вызывается после активации подписки: без этого листинги, упёршиеся
        в лимит во время неактивной подписки, остаются в тупиковом статусе.
        """
        from apps.billing.outbox import enqueue_limit_reached_requeue

        enqueue_limit_reached_requeue(
            tenant=tenant,
            invoice=invoice,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    @transaction.atomic
    def handle_payment_failed_webhook(
        invoice_id: int,
        *,
        payment_id: str,
    ) -> bool:
        """
        Обрабатывает вебхук payment.canceled от YooKassa.

        Переводит Invoice в статус failed.
        """
        try:
            invoice = Invoice.objects.select_for_update().select_related('tenant').get(
                pk=invoice_id,
            )
        except Invoice.DoesNotExist:
            return False
        if not payment_id or invoice.yookassa_payment_id != payment_id:
            logger.error(
                'Invoice %s не принадлежит payment_id=%s',
                invoice.pk,
                payment_id,
            )
            return False
        if invoice.status == Invoice.STATUS_FAILED:
            return True
        if invoice.status != Invoice.STATUS_PENDING:
            logger.warning(
                'Отмена payment_id=%s не меняет invoice=%s в статусе %s',
                payment_id,
                invoice.pk,
                invoice.status,
            )
            return False
        invoice.status = Invoice.STATUS_FAILED
        invoice.save(update_fields=['status', 'updated_at'])
        _write_billing_log(
            invoice.tenant,
            'warn',
            f'Оплата не прошла (payment_id={payment_id})',
        )
        logger.warning('Вебхук payment.canceled: payment_id=%s', payment_id)
        return True

    @staticmethod
    @transaction.atomic
    def handle_reversal_success(
        *,
        invoice_id: int,
        provider_reference: str,
        payment_id: str,
        amount,
        currency: str,
        kind: str = PaymentReversal.KIND_REFUND,
    ) -> PaymentReversal | None:
        """Применяет успешный refund/chargeback как неизменяемую обратную операцию."""
        if not provider_reference or len(provider_reference) > 200:
            logger.error('Возврат: некорректный provider_reference.')
            return None
        try:
            invoice = Invoice.objects.select_for_update().select_related('tenant').get(
                pk=invoice_id,
            )
        except Invoice.DoesNotExist:
            logger.error('Возврат %s: invoice=%s не найден', provider_reference, invoice_id)
            return None
        if not payment_id or invoice.yookassa_payment_id != payment_id:
            logger.error(
                'Возврат %s: invoice=%s не принадлежит payment_id=%s',
                provider_reference,
                invoice.pk,
                payment_id,
            )
            return None
        try:
            raw_amount = Decimal(str(amount))
            reversal_amount = raw_amount.quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError, ValueError):
            logger.error('Возврат %s: некорректная сумма %r', provider_reference, amount)
            return None

        received_currency = str(currency or '').upper()
        if (
            not raw_amount.is_finite()
            or raw_amount <= 0
            or raw_amount != reversal_amount
            or len(received_currency) != 3
            or not received_currency.isalpha()
        ):
            logger.error('Возврат %s: некорректная сумма или валюта', provider_reference)
            return None

        existing = PaymentReversal.objects.filter(
            provider_reference=provider_reference,
        ).first()
        if existing is not None:
            if _reversal_matches(
                existing,
                invoice=invoice,
                provider_reference=provider_reference,
                payment_id=payment_id,
                amount=reversal_amount,
                currency=received_currency,
                kind=kind,
            ):
                return existing
            logger.error(
                'Повтор provider_reference=%s не совпадает с сохранённым возвратом.',
                provider_reference,
            )
            return None

        refundable_statuses = {
            Invoice.STATUS_PAID,
            Invoice.STATUS_PARTIALLY_REFUNDED,
            Invoice.STATUS_MANUAL_REVIEW,
        }
        if invoice.status not in refundable_statuses:
            reversal, created = _create_reversal_fail_closed(
                invoice=invoice,
                kind=kind,
                provider_reference=provider_reference,
                payment_id=payment_id,
                amount=reversal_amount,
                currency=received_currency,
                status=PaymentReversal.STATUS_MANUAL_REVIEW,
                reason='Исходный Invoice не находился в оплачиваемом статусе.',
            )
            if reversal is None or not created:
                return reversal
            invoice.refund_review_required = True
            invoice.status = Invoice.STATUS_MANUAL_REVIEW
            invoice.save(update_fields=[
                'refund_review_required', 'status', 'updated_at',
            ])
            return reversal

        if received_currency != invoice.currency:
            reversal, created = _create_reversal_fail_closed(
                invoice=invoice,
                kind=kind,
                provider_reference=provider_reference,
                payment_id=payment_id,
                amount=reversal_amount,
                currency=received_currency,
                status=PaymentReversal.STATUS_MANUAL_REVIEW,
                reason='Некорректная сумма или валюта возврата.',
            )
            if reversal is None or not created:
                return reversal
            invoice.refund_review_required = True
            invoice.status = Invoice.STATUS_MANUAL_REVIEW
            invoice.save(update_fields=[
                'refund_review_required', 'status', 'updated_at',
            ])
            return reversal

        remaining_refundable = invoice.amount - invoice.refunded_amount
        if reversal_amount > remaining_refundable:
            reversal, created = _create_reversal_fail_closed(
                invoice=invoice,
                kind=kind,
                provider_reference=provider_reference,
                payment_id=payment_id,
                amount=reversal_amount,
                currency=received_currency,
                status=PaymentReversal.STATUS_MANUAL_REVIEW,
                reason='Сумма возвратов превышает сумму исходного платежа.',
            )
            if reversal is None or not created:
                return reversal
            invoice.refund_review_required = True
            invoice.status = Invoice.STATUS_MANUAL_REVIEW
            invoice.save(update_fields=[
                'refund_review_required', 'status', 'updated_at',
            ])
            return reversal

        new_refunded_amount = invoice.refunded_amount + reversal_amount
        reversal, created = _create_reversal_fail_closed(
            invoice=invoice,
            kind=kind,
            provider_reference=provider_reference,
            payment_id=payment_id,
            amount=reversal_amount,
            currency=received_currency,
            status=PaymentReversal.STATUS_MANUAL_REVIEW,
        )
        if reversal is None or not created:
            return reversal

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

        credit_entitlement, entitlement_snapshot = (
            BillingService._ai_entitlement_from_invoice(invoice)
        )
        if credit_entitlement is None:
            reversal.reason = 'Нельзя определить исходно купленные кредиты.'
            reversal.save(update_fields=['reason', 'updated_at'])
            invoice.refunded_amount = new_refunded_amount
            invoice.refund_review_required = True
            invoice.status = Invoice.STATUS_MANUAL_REVIEW
            invoice.save(update_fields=[
                'refunded_amount', 'refund_review_required', 'status', 'updated_at',
            ])
            return reversal
        if not invoice.entitlement_snapshot and entitlement_snapshot:
            invoice.entitlement_snapshot = entitlement_snapshot

        previous_requested = (
            PaymentReversal.objects.filter(invoice=invoice)
            .exclude(pk=reversal.pk)
            .aggregate(total=Sum('credits_requested'))['total']
            or Decimal('0')
        )
        if new_refunded_amount == invoice.amount:
            cumulative_credit_target = credit_entitlement
        else:
            cumulative_credit_target = (
                credit_entitlement * new_refunded_amount / invoice.amount
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
            'refunded_amount', 'refund_review_required', 'status',
            'entitlement_snapshot', 'updated_at',
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
            invoice_id=invoice.pk,
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
            billing_version=1,
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
        sub.billing_version += 1
        sub.save(update_fields=[
            'status', 'current_period_start', 'current_period_end',
            'ai_period_start', 'ai_period_end', 'cancelled_at',
            'billing_version', 'updated_at',
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

        sub = Subscription.objects.select_for_update().get(tenant_id=tenant.pk)
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
        sub.billing_version += 1
        sub.save()
        BillingService.sync_tenant_trial_end(sub)

        # Новый расчётный период — начисляем включённый AI-баланс.
        Tenant.objects.filter(pk=tenant.pk).update(ai_credits_used=0)
        tenant.ai_credits_used = 0
        # ``tenant.subscription`` is commonly cached by trial creation. Do not
        # calculate limits or return control with the previous plan attached.
        tenant._state.fields_cache.pop('subscription', None)
        from apps.billing.ai_wallet import AIWalletService
        AIWalletService.grant_included(
            tenant,
            AIWalletService.effective_limit(tenant),
            period_end=sub.ai_period_end,
            idempotency_key=(
                f'subscription-grant:{sub.pk}:v{sub.billing_version}'
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
        sub.billing_version += 1
        sub.save(update_fields=[
            'ai_period_start', 'ai_period_end', 'billing_version', 'updated_at',
        ])

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
    def check_expired_trials() -> int:
        """
        Переводит просроченные trial/active-подписки в past_due.
        Вызывается Celery Beat ежедневно.
        Возвращает количество обработанных подписок.
        """
        today = timezone.localdate()
        expired = list(Subscription.objects.select_for_update().select_related(
            'tenant',
        ).filter(
            status__in=(Subscription.STATUS_TRIAL, Subscription.STATUS_ACTIVE),
            current_period_end__lt=today,
        ).order_by('pk'))
        from apps.billing.outbox import enqueue_notification

        for subscription in expired:
            subscription.status = Subscription.STATUS_PAST_DUE
            subscription.billing_version += 1
            subscription.save(update_fields=[
                'status', 'billing_version', 'updated_at',
            ])
            Tenant.objects.filter(pk=subscription.tenant_id).update(
                trial_ends_at=None,
            )
            enqueue_notification(
                tenant=subscription.tenant,
                level='billing',
                message=(
                    'Ваша подписка MAP истекла. Продлите подписку в течение '
                    f'{GRACE_PERIOD_DAYS} дней.'
                ),
                idempotency_key=(
                    f'subscription:{subscription.pk}:past-due:'
                    f'{subscription.current_period_end}:v1'
                ),
            )
        count = len(expired)
        if count:
            logger.warning('Переведено %d истёкших подписок в past_due', count)
        return count

    @staticmethod
    @transaction.atomic
    def check_grace_period_expired() -> int:
        """
        Отменяет подписки, у которых истёк grace period (7 дней past_due).
        Вызывается Celery Beat ежедневно.
        """
        deadline = timezone.localdate() - timedelta(days=GRACE_PERIOD_DAYS)
        expired = list(Subscription.objects.select_for_update().select_related(
            'tenant',
        ).filter(
            status=Subscription.STATUS_PAST_DUE,
            current_period_end__lt=deadline,
        ).order_by('pk'))
        from apps.billing.outbox import enqueue_notification

        cancelled_at = timezone.now()
        for subscription in expired:
            subscription.status = Subscription.STATUS_CANCELLED
            subscription.cancelled_at = cancelled_at
            subscription.billing_version += 1
            subscription.save(update_fields=[
                'status', 'cancelled_at', 'billing_version', 'updated_at',
            ])
            enqueue_notification(
                tenant=subscription.tenant,
                level='critical',
                message=(
                    'Ваша подписка MAP отменена. '
                    'Публикация новых объявлений заблокирована.'
                ),
                idempotency_key=(
                    f'subscription:{subscription.pk}:cancelled:'
                    f'{subscription.current_period_end}:v1'
                ),
            )
        count = len(expired)
        if count:
            logger.warning('Отменено %d подписок после grace period', count)
        return count
