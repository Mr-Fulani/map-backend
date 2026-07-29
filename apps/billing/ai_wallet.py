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

    @staticmethod
    def _plan_limit(tenant) -> Decimal | None:
        try:
            limit = tenant.subscription.plan.limit_ai_credits
        except Subscription.DoesNotExist:
            return Decimal('0')
        return None if limit is None else Decimal(limit)

    @classmethod
    def is_unlimited(cls, tenant) -> bool:
        return cls._plan_limit(tenant) is None

    @classmethod
    def ensure_wallet(cls, tenant) -> AIWallet:
        wallet, created = AIWallet.objects.get_or_create(tenant=tenant)
        if not created:
            return wallet

        limit = cls._plan_limit(tenant)
        if limit is not None:
            remaining = max(Decimal('0'), limit - Decimal(tenant.ai_credits_used))
            wallet.included_balance = remaining
            try:
                wallet.included_expires_at = timezone.make_aware(
                    datetime.combine(tenant.subscription.current_period_end, time.max),
                )
            except (Subscription.DoesNotExist, TypeError):
                pass
            wallet.save(update_fields=['included_balance', 'included_expires_at'])
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
        return {
            'included': wallet.included_balance,
            'purchased': wallet.purchased_balance,
            'reserved': wallet.reserved_balance,
            'total': wallet.total_balance,
            'available': wallet.available_balance,
            'included_expires_at': wallet.included_expires_at,
            'unlimited': cls.is_unlimited(tenant),
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
        if cls.is_unlimited(tenant):
            return AIReservation(reservation_key, amount, unlimited=True)
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
        wallet.included_balance = grant_amount
        wallet.reserved_balance = Decimal('0')
        wallet.included_expires_at = (
            timezone.make_aware(datetime.combine(period_end, time.max))
            if period_end else None
        )
        wallet.save(update_fields=[
            'included_balance', 'reserved_balance', 'included_expires_at', 'updated_at',
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
