from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.billing.ai_wallet import AIWalletService, InsufficientAICredits
from apps.billing.models import AICreditTransaction, Plan, Subscription
from apps.billing.services import BillingService, LimitChecker
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(
        name=slug,
        slug=slug,
        owner_email=f'{slug}@test.com',
        owner_password='pass12345',
    )
    return tenant


@pytest.mark.django_db
def test_enterprise_has_finite_monthly_limit():
    tenant = make_tenant('enterprise-finite-ai')

    BillingService.upgrade_plan(
        tenant,
        Plan.SLUG_ENTERPRISE,
        Subscription.PERIOD_MONTHLY,
    )

    summary = AIWalletService.summary(tenant)
    assert tenant.subscription.plan.limit_ai_credits == 50_000
    assert summary['included_limit'] == Decimal('50000')
    assert summary['included'] == Decimal('50000')
    assert summary['unlimited'] is False


@pytest.mark.django_db
def test_tenant_override_changes_current_limit_without_restoring_spent_credits():
    tenant = make_tenant('enterprise-custom-ai')
    BillingService.upgrade_plan(
        tenant,
        Plan.SLUG_ENTERPRISE,
        Subscription.PERIOD_MONTHLY,
    )
    reservation = AIWalletService.reserve(
        tenant,
        Decimal('10000'),
        key='enterprise-custom-ai:initial-spend',
    )
    AIWalletService.settle(tenant, reservation, Decimal('10000'))

    tenant.ai_credit_limit_override = 60_000
    tenant.save(update_fields=['ai_credit_limit_override'])
    AIWalletService.sync_included_limit(tenant)

    summary = AIWalletService.summary(tenant)
    assert summary['included_limit'] == Decimal('60000')
    assert summary['included'] == Decimal('50000')
    assert summary['included_used'] == Decimal('10000')
    assert summary['individual_limit'] is True
    assert AICreditTransaction.objects.filter(
        tenant=tenant,
        kind=AICreditTransaction.KIND_ADJUSTMENT,
        reference='tenant-limit-override',
    ).exists()


@pytest.mark.django_db
def test_lower_override_preserves_already_reserved_provider_cost():
    tenant = make_tenant('enterprise-reserved-ai')
    AIWalletService.grant_included(
        tenant,
        Decimal('100'),
        period_end=tenant.subscription.ai_period_end,
        idempotency_key='enterprise-reserved-ai:grant',
    )
    reservation = AIWalletService.reserve(
        tenant,
        Decimal('60'),
        key='enterprise-reserved-ai:reservation',
    )

    tenant.ai_credit_limit_override = 10
    tenant.save(update_fields=['ai_credit_limit_override'])
    AIWalletService.sync_included_limit(tenant)

    before_settlement = AIWalletService.summary(tenant)
    assert before_settlement['included_limit'] == Decimal('10')
    assert before_settlement['included'] == Decimal('60')
    assert before_settlement['reserved'] == Decimal('60')

    with patch('apps.notifications.tasks.send_notification_task.delay'):
        charged = AIWalletService.settle(tenant, reservation, Decimal('60'))

    assert charged == Decimal('60')
    assert AIWalletService.summary(tenant)['available'] == Decimal('0')


@pytest.mark.django_db
def test_purchased_credits_are_used_after_included_package():
    tenant = make_tenant('enterprise-overage-ai')
    AIWalletService.grant_included(
        tenant,
        Decimal('10'),
        period_end=tenant.subscription.ai_period_end,
        idempotency_key='enterprise-overage-ai:grant',
    )
    AIWalletService.topup(
        tenant,
        Decimal('5'),
        idempotency_key='enterprise-overage-ai:topup',
    )

    with patch('apps.notifications.tasks.send_notification_task.delay'):
        reservation = AIWalletService.reserve(
            tenant,
            Decimal('12'),
            key='enterprise-overage-ai:spend',
        )
        charged = AIWalletService.settle(tenant, reservation, Decimal('12'))

    summary = AIWalletService.summary(tenant)
    assert charged == Decimal('12')
    assert summary['included'] == Decimal('0')
    assert summary['purchased'] == Decimal('3')
    assert summary['overage_active'] is True
    assert summary['threshold'] == 'exhausted'

    final_reservation = AIWalletService.reserve(
        tenant,
        Decimal('3'),
        key='enterprise-overage-ai:final-spend',
    )
    AIWalletService.settle(tenant, final_reservation, Decimal('3'))
    with pytest.raises(InsufficientAICredits):
        AIWalletService.reserve(
            tenant,
            Decimal('0.0001'),
            key='enterprise-overage-ai:blocked',
        )


@pytest.mark.django_db(transaction=True)
def test_threshold_notifications_are_sent_once_per_period():
    tenant = make_tenant('enterprise-ai-thresholds')
    AIWalletService.grant_included(
        tenant,
        Decimal('100'),
        period_end=tenant.subscription.ai_period_end,
        idempotency_key='enterprise-ai-thresholds:grant',
    )

    with patch('apps.notifications.tasks.send_notification_task.delay') as notify:
        for amount, key in [
            (Decimal('85'), 'threshold:80'),
            (Decimal('7'), 'threshold:90'),
            (Decimal('1'), 'threshold:dedupe'),
            (Decimal('7'), 'threshold:100'),
        ]:
            reservation = AIWalletService.reserve(tenant, amount, key=key)
            AIWalletService.settle(tenant, reservation, amount)

    assert notify.call_count == 3
    messages = [call.args[2] for call in notify.call_args_list]
    assert '80%' in messages[0]
    assert '90%' in messages[1]
    assert 'исчерпаны' in messages[2]

    wallet = AIWalletService.ensure_wallet(tenant)
    assert wallet.notification_state == {'sent_thresholds': [80, 90, 100]}


@pytest.mark.django_db
def test_usage_summary_exposes_limit_and_threshold_state():
    tenant = make_tenant('enterprise-ai-usage')
    tenant.ai_credit_limit_override = 100
    tenant.save(update_fields=['ai_credit_limit_override'])
    AIWalletService.sync_included_limit(tenant)

    reservation = AIWalletService.reserve(
        tenant,
        Decimal('80'),
        key='enterprise-ai-usage:spend',
    )
    with patch('apps.notifications.tasks.send_notification_task.delay'):
        AIWalletService.settle(tenant, reservation, Decimal('80'))

    credits = LimitChecker().get_usage_summary(tenant)['ai_credits']
    assert credits['used'] == Decimal('80')
    assert credits['limit'] == Decimal('100')
    assert credits['included_percent_used'] == Decimal('80.00')
    assert credits['threshold'] == 'warning'
    assert credits['individual_limit'] is True
