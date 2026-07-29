from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from apps.billing.models import AICreditTransaction, AIWallet, Subscription


class InsufficientAICredits(Exception):
    """Недостаточно доступных AI-кредитов для резерва."""


@dataclass(frozen=True)
class AIReservation:
    key: str
    amount: Decimal
    unlimited: bool = False


class AIWalletService:
    """Атомарное начисление, резервирование и списание AI-кредитов."""

    WARNING_THRESHOLDS = (80, 90, 100)

    @staticmethod
    def effective_limit(tenant) -> Decimal:
        """Возвращает договорной месячный лимит, не допуская AI-безлимита."""
        override = tenant.ai_credit_limit_override
        if override is not None:
            return Decimal(override)
        try:
            limit = tenant.subscription.plan.limit_ai_credits
        except Subscription.DoesNotExist:
            return Decimal('0')
        return Decimal('0') if limit is None else Decimal(limit)

    @classmethod
    def is_unlimited(cls, tenant) -> bool:
        """Оставлено для обратной совместимости API; AI-безлимит запрещён."""
        return False

    @classmethod
    def ensure_wallet(cls, tenant) -> AIWallet:
        wallet, created = AIWallet.objects.get_or_create(tenant=tenant)
        if not created:
            return wallet

        limit = cls.effective_limit(tenant)
        remaining = max(Decimal('0'), limit - Decimal(tenant.ai_credits_used))
        wallet.included_limit = limit
        wallet.included_balance = remaining
        try:
            period_end = (
                tenant.subscription.ai_period_end
                or tenant.subscription.current_period_end
            )
            wallet.included_expires_at = timezone.make_aware(
                datetime.combine(period_end, time.max),
            )
        except (Subscription.DoesNotExist, TypeError):
            pass
        wallet.save(update_fields=[
            'included_limit', 'included_balance', 'included_expires_at',
        ])
        if remaining:
            AICreditTransaction.objects.create(
                wallet=wallet,
                tenant=tenant,
                kind=AICreditTransaction.KIND_GRANT,
                balance_type=AICreditTransaction.BALANCE_INCLUDED,
                amount=remaining,
                idempotency_key=f'wallet-bootstrap:{tenant.pk}',
                reference='legacy-ai-credits',
            )
        return wallet

    @classmethod
    def summary(cls, tenant) -> dict:
        wallet = cls.ensure_wallet(tenant)
        wallet.refresh_from_db()
        limit = wallet.included_limit
        used = max(Decimal('0'), limit - wallet.included_balance)
        percent_used = (
            min(Decimal('100'), (used / limit * Decimal('100')))
            if limit > 0 else Decimal('100')
        )
        return {
            'included': wallet.included_balance,
            'included_limit': limit,
            'included_used': used,
            'included_percent_used': percent_used.quantize(Decimal('0.01')),
            'purchased': wallet.purchased_balance,
            'reserved': wallet.reserved_balance,
            'total': wallet.total_balance,
            'available': wallet.available_balance,
            'included_expires_at': wallet.included_expires_at,
            'unlimited': False,
            'individual_limit': tenant.ai_credit_limit_override is not None,
            'overage_active': (
                wallet.included_balance <= 0 and wallet.purchased_balance > 0
            ),
            'threshold': cls._threshold_state(percent_used),
        }

    @classmethod
    @transaction.atomic
    def reserve(
        cls,
        tenant,
        amount: Decimal,
        *,
        key: str | None = None,
        details: dict | None = None,
    ) -> AIReservation:
        amount = max(Decimal('0'), Decimal(amount))
        reservation_key = key or f'ai-reserve:{uuid4()}'
        wallet = cls.ensure_wallet(tenant)
        wallet = AIWallet.objects.select_for_update().get(pk=wallet.pk)

        cls._expire_included_if_needed(wallet)
        if wallet.available_balance < amount:
            raise InsufficientAICredits(
                f'Недостаточно AI-кредитов: доступно {wallet.available_balance}, требуется {amount}.'
            )

        wallet.reserved_balance += amount
        wallet.save(update_fields=['reserved_balance', 'updated_at'])
        AICreditTransaction.objects.create(
            wallet=wallet,
            tenant=tenant,
            kind=AICreditTransaction.KIND_RESERVE,
            balance_type=AICreditTransaction.BALANCE_RESERVED,
            amount=amount,
            idempotency_key=reservation_key,
            details=details or {},
        )
        return AIReservation(reservation_key, amount)

    @classmethod
    @transaction.atomic
    def release(cls, tenant, reservation: AIReservation, *, reason: str = '') -> None:
        if reservation.unlimited:
            return
        wallet = AIWallet.objects.select_for_update().get(tenant=tenant)
        released = min(wallet.reserved_balance, reservation.amount)
        wallet.reserved_balance -= released
        wallet.save(update_fields=['reserved_balance', 'updated_at'])
        AICreditTransaction.objects.get_or_create(
            tenant=tenant,
            idempotency_key=f'{reservation.key}:release',
            defaults={
                'wallet': wallet,
                'kind': AICreditTransaction.KIND_RELEASE,
                'balance_type': AICreditTransaction.BALANCE_RESERVED,
                'amount': -released,
                'reference': reservation.key,
                'details': {'reason': reason},
            },
        )

    @classmethod
    @transaction.atomic
    def settle(
        cls,
        tenant,
        reservation: AIReservation,
        actual_amount: Decimal,
        *,
        details: dict | None = None,
    ) -> Decimal:
        actual_amount = max(Decimal('0'), Decimal(actual_amount))
        if reservation.unlimited:
            return actual_amount

        wallet = AIWallet.objects.select_for_update().get(tenant=tenant)
        if AICreditTransaction.objects.filter(
            tenant=tenant,
            idempotency_key=f'{reservation.key}:settled',
        ).exists():
            return actual_amount

        wallet.reserved_balance = max(
            Decimal('0'), wallet.reserved_balance - reservation.amount,
        )
        # Не расходуем резерв параллельных запросов. Собственный резерв уже снят,
        # поэтому доступно всё, кроме оставшегося reserved_balance.
        available_for_charge = max(
            Decimal('0'),
            wallet.included_balance + wallet.purchased_balance - wallet.reserved_balance,
        )
        charged = min(actual_amount, available_for_charge)

        from_included = min(wallet.included_balance, charged)
        wallet.included_balance -= from_included
        from_purchased = charged - from_included
        wallet.purchased_balance -= from_purchased
        wallet.save(update_fields=[
            'included_balance', 'purchased_balance', 'reserved_balance', 'updated_at',
        ])
        cls._notify_threshold_if_needed(wallet)

        base_details = details or {}
        if from_included:
            AICreditTransaction.objects.create(
                wallet=wallet,
                tenant=tenant,
                kind=AICreditTransaction.KIND_CHARGE,
                balance_type=AICreditTransaction.BALANCE_INCLUDED,
                amount=-from_included,
                idempotency_key=f'{reservation.key}:charge:included',
                reference=reservation.key,
                details=base_details,
            )
        if from_purchased:
            AICreditTransaction.objects.create(
                wallet=wallet,
                tenant=tenant,
                kind=AICreditTransaction.KIND_CHARGE,
                balance_type=AICreditTransaction.BALANCE_PURCHASED,
                amount=-from_purchased,
                idempotency_key=f'{reservation.key}:charge:purchased',
                reference=reservation.key,
                details=base_details,
            )
        AICreditTransaction.objects.create(
            wallet=wallet,
            tenant=tenant,
            kind=AICreditTransaction.KIND_RELEASE,
            balance_type=AICreditTransaction.BALANCE_RESERVED,
            amount=-reservation.amount,
            idempotency_key=f'{reservation.key}:settled',
            reference=reservation.key,
            details={'actual_credits': str(actual_amount)},
        )
        return charged

    @classmethod
    @transaction.atomic
    def grant_included(
        cls,
        tenant,
        amount: Decimal | None,
        *,
        period_end: date | None,
        idempotency_key: str,
    ) -> AIWallet:
        wallet = cls.ensure_wallet(tenant)
        wallet = AIWallet.objects.select_for_update().get(pk=wallet.pk)
        if AICreditTransaction.objects.filter(
            tenant=tenant, idempotency_key=idempotency_key,
        ).exists():
            return wallet

        if wallet.included_balance:
            AICreditTransaction.objects.create(
                wallet=wallet,
                tenant=tenant,
                kind=AICreditTransaction.KIND_EXPIRE,
                balance_type=AICreditTransaction.BALANCE_INCLUDED,
                amount=-wallet.included_balance,
                idempotency_key=f'{idempotency_key}:expire',
            )
        grant_amount = Decimal('0') if amount is None else max(Decimal('0'), Decimal(amount))
        wallet.included_limit = grant_amount
        wallet.included_balance = grant_amount
        wallet.included_expires_at = (
            timezone.make_aware(datetime.combine(period_end, time.max))
            if period_end else None
        )
        wallet.notification_state = {}
        wallet.save(update_fields=[
            'included_limit', 'included_balance', 'included_expires_at',
            'notification_state', 'updated_at',
        ])
        AICreditTransaction.objects.create(
            wallet=wallet,
            tenant=tenant,
            kind=AICreditTransaction.KIND_GRANT,
            balance_type=AICreditTransaction.BALANCE_INCLUDED,
            amount=grant_amount,
            idempotency_key=idempotency_key,
            reference='subscription-period',
        )
        return wallet

    @classmethod
    @transaction.atomic
    def sync_included_limit(cls, tenant) -> AIWallet:
        """Применяет индивидуальный лимит в текущем периоде, сохраняя уже потраченное."""
        wallet = cls.ensure_wallet(tenant)
        wallet = AIWallet.objects.select_for_update().get(pk=wallet.pk)
        new_limit = cls.effective_limit(tenant)
        consumed = max(Decimal('0'), wallet.included_limit - wallet.included_balance)
        new_balance = max(Decimal('0'), new_limit - consumed)
        # Уже подтверждённые резервы нельзя обесценить снижением лимита:
        # провайдер мог начать выполнять запрос, и его стоимость нужно списать.
        reserved_from_included = max(
            Decimal('0'),
            wallet.reserved_balance - wallet.purchased_balance,
        )
        new_balance = max(new_balance, reserved_from_included)
        adjustment = new_balance - wallet.included_balance

        wallet.included_limit = new_limit
        wallet.included_balance = new_balance
        wallet.notification_state = {}
        wallet.save(update_fields=[
            'included_limit', 'included_balance', 'notification_state', 'updated_at',
        ])
        if adjustment:
            AICreditTransaction.objects.create(
                wallet=wallet,
                tenant=tenant,
                kind=AICreditTransaction.KIND_ADJUSTMENT,
                balance_type=AICreditTransaction.BALANCE_INCLUDED,
                amount=adjustment,
                idempotency_key=f'limit-sync:{tenant.pk}:{uuid4()}',
                reference='tenant-limit-override',
                details={
                    'new_limit': str(new_limit),
                    'consumed': str(consumed),
                    'preserved_for_reservations': str(reserved_from_included),
                },
            )
        return wallet

    @classmethod
    @transaction.atomic
    def topup(
        cls,
        tenant,
        amount: Decimal,
        *,
        idempotency_key: str,
        reference: str = '',
    ) -> AIWallet:
        wallet = cls.ensure_wallet(tenant)
        wallet = AIWallet.objects.select_for_update().get(pk=wallet.pk)
        if AICreditTransaction.objects.filter(
            tenant=tenant, idempotency_key=idempotency_key,
        ).exists():
            return wallet
        amount = max(Decimal('0'), Decimal(amount))
        wallet.purchased_balance += amount
        wallet.save(update_fields=['purchased_balance', 'updated_at'])
        AICreditTransaction.objects.create(
            wallet=wallet,
            tenant=tenant,
            kind=AICreditTransaction.KIND_TOPUP,
            balance_type=AICreditTransaction.BALANCE_PURCHASED,
            amount=amount,
            idempotency_key=idempotency_key,
            reference=reference,
        )
        return wallet

    @staticmethod
    def _expire_included_if_needed(wallet: AIWallet) -> None:
        if (
            wallet.included_expires_at
            and wallet.included_expires_at < timezone.now()
            and wallet.included_balance > 0
        ):
            expired = wallet.included_balance
            wallet.included_balance = Decimal('0')
            wallet.save(update_fields=['included_balance', 'updated_at'])
            AICreditTransaction.objects.create(
                wallet=wallet,
                tenant=wallet.tenant,
                kind=AICreditTransaction.KIND_EXPIRE,
                balance_type=AICreditTransaction.BALANCE_INCLUDED,
                amount=-expired,
                idempotency_key=f'included-expire:{wallet.pk}:{wallet.included_expires_at.isoformat()}',
            )

    @staticmethod
    def _threshold_state(percent_used: Decimal) -> str:
        if percent_used >= 100:
            return 'exhausted'
        if percent_used >= 90:
            return 'critical'
        if percent_used >= 80:
            return 'warning'
        return 'normal'

    @classmethod
    def _notify_threshold_if_needed(cls, wallet: AIWallet) -> None:
        """Ставит одно уведомление на каждый достигнутый порог расчётного периода."""
        limit = wallet.included_limit
        if limit <= 0:
            percent_used = Decimal('100')
        else:
            used = max(Decimal('0'), limit - wallet.included_balance)
            percent_used = min(Decimal('100'), used / limit * Decimal('100'))

        reached = [
            threshold for threshold in cls.WARNING_THRESHOLDS
            if percent_used >= threshold
        ]
        state = dict(wallet.notification_state or {})
        sent = {int(value) for value in state.get('sent_thresholds', [])}
        unsent = [threshold for threshold in reached if threshold not in sent]
        if not unsent:
            return

        threshold = max(unsent)
        sent.update(reached)
        wallet.notification_state = {'sent_thresholds': sorted(sent)}
        wallet.save(update_fields=['notification_state', 'updated_at'])

        from apps.notifications.services import LEVEL_BILLING, LEVEL_CRITICAL
        from apps.notifications.tasks import send_notification_task

        if threshold >= 100:
            level = LEVEL_CRITICAL
            if wallet.purchased_balance > 0:
                message = (
                    'Включённый месячный пакет AI-кредитов исчерпан. '
                    'Дальнейшие запросы оплачиваются из купленного баланса.'
                )
            else:
                message = (
                    'AI-кредиты исчерпаны. AI-запросы приостановлены до пополнения '
                    'или следующего расчётного периода.'
                )
        else:
            level = LEVEL_BILLING
            message = (
                f'Использовано {threshold}% месячного пакета AI-кредитов. '
                f'Осталось {wallet.included_balance.normalize()} кредитов.'
            )

        tenant_id = wallet.tenant_id
        transaction.on_commit(
            lambda: send_notification_task.delay(tenant_id, level, message),
        )
