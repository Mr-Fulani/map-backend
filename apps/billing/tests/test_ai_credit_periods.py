from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.billing.ai_wallet import AIWalletService
from apps.billing.models import AICreditTransaction, Plan, Subscription
from apps.billing.services import (
    BillingService, add_billing_months, ai_credit_period_for_date,
)
from apps.billing.tasks import reset_monthly_ai_credits
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(
        name=slug,
        slug=slug,
        owner_email=f'{slug}@test.com',
        owner_password='pass12345',
    )
    return tenant


@pytest.mark.parametrize(
    ('anchor', 'one_month', 'two_months'),
    [
        (date(2026, 1, 28), date(2026, 2, 28), date(2026, 3, 28)),
        (date(2024, 1, 29), date(2024, 2, 29), date(2024, 3, 29)),
        (date(2026, 1, 30), date(2026, 2, 28), date(2026, 3, 30)),
        (date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)),
    ],
)
def test_billing_months_preserve_original_anchor_day(
    anchor,
    one_month,
    two_months,
):
    assert add_billing_months(anchor, 1) == one_month
    assert add_billing_months(anchor, 2) == two_months


@pytest.mark.django_db
def test_yearly_subscription_has_twelve_unique_monthly_ai_grants():
    tenant = make_tenant('annual-ai-periods')
    initial_grants = AICreditTransaction.objects.filter(
        tenant=tenant,
        kind=AICreditTransaction.KIND_GRANT,
    ).count()
    initial_keys = set(
        AICreditTransaction.objects.filter(
            tenant=tenant,
            kind=AICreditTransaction.KIND_GRANT,
        ).values_list('idempotency_key', flat=True)
    )

    with patch(
        'apps.billing.services.timezone.localdate',
        return_value=date(2026, 1, 31),
    ):
        BillingService.upgrade_plan(
            tenant,
            Plan.SLUG_BUSINESS,
            Subscription.PERIOD_YEARLY,
        )

    rollover_dates = [
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
        date(2026, 5, 31),
        date(2026, 6, 30),
        date(2026, 7, 31),
        date(2026, 8, 31),
        date(2026, 9, 30),
        date(2026, 10, 31),
        date(2026, 11, 30),
        date(2026, 12, 31),
    ]
    for rollover_date in rollover_dates:
        with patch(
            'apps.billing.tasks.timezone.localdate',
            return_value=rollover_date,
        ):
            assert reset_monthly_ai_credits()['reset_count'] == 1
            assert reset_monthly_ai_credits()['reset_count'] == 0

    subscription = Subscription.objects.get(tenant=tenant)
    grant_transactions = AICreditTransaction.objects.filter(
        tenant=tenant,
        kind=AICreditTransaction.KIND_GRANT,
    )
    annual_grants = grant_transactions.count() - initial_grants
    annual_keys = set(
        grant_transactions.filter(
            idempotency_key__startswith=f'subscription-grant:{subscription.pk}:',
        ).values_list('idempotency_key', flat=True)
    ) - initial_keys

    assert annual_grants == 12
    assert len(annual_keys) == 12
    assert subscription.ai_period_start == date(2026, 12, 31)
    assert subscription.ai_period_end == date(2027, 1, 31)


@pytest.mark.django_db
def test_rollover_preserves_purchased_balance_and_active_reservations():
    tenant = make_tenant('annual-ai-wallet')
    with patch(
        'apps.billing.services.timezone.localdate',
        return_value=date(2026, 1, 30),
    ):
        BillingService.upgrade_plan(
            tenant,
            Plan.SLUG_STARTER,
            Subscription.PERIOD_YEARLY,
        )

    AIWalletService.topup(
        tenant,
        Decimal('1000'),
        idempotency_key='annual-ai-wallet:topup',
    )
    AIWalletService.reserve(
        tenant,
        Decimal('40'),
        key='annual-ai-wallet:reservation',
    )
    wallet = AIWalletService.ensure_wallet(tenant)
    wallet.included_balance = Decimal('123')
    wallet.save(update_fields=['included_balance'])
    tenant.ai_credits_used = 777
    tenant.save(update_fields=['ai_credits_used'])

    with patch(
        'apps.billing.tasks.timezone.localdate',
        return_value=date(2026, 2, 28),
    ):
        first = reset_monthly_ai_credits()
        second = reset_monthly_ai_credits()

    summary = AIWalletService.summary(tenant)
    tenant.refresh_from_db()
    assert first['reset_count'] == 1
    assert second['reset_count'] == 0
    assert summary['included'] == Decimal('1000')
    assert summary['purchased'] == Decimal('1000')
    assert summary['reserved'] == Decimal('40')
    assert tenant.ai_credits_used == 0


@pytest.mark.django_db
def test_yearly_period_lookup_returns_current_month_not_full_year():
    tenant = make_tenant('annual-period-lookup')
    subscription = tenant.subscription
    subscription.billing_period = Subscription.PERIOD_YEARLY
    subscription.current_period_start = date(2026, 1, 31)
    subscription.current_period_end = date(2027, 1, 31)

    assert ai_credit_period_for_date(
        subscription,
        date(2026, 2, 27),
    ) == (date(2026, 1, 31), date(2026, 2, 28))
    assert ai_credit_period_for_date(
        subscription,
        date(2026, 2, 28),
    ) == (date(2026, 2, 28), date(2026, 3, 31))
