from datetime import date, timedelta

import pytest

from apps.billing.models import Plan, Subscription
from apps.billing.services import BillingService, LimitChecker
from apps.tenants.services import TenantService


def make_tenant(slug, email):
    """Вспомогательная функция для создания тенанта с trial."""
    tenant, _ = TenantService.create_tenant(
        name=slug, slug=slug, owner_email=email, owner_password='pass12345',
    )
    return tenant


@pytest.mark.django_db
class TestBillingService:
    def test_trial_is_created_on_registration(self):
        """При регистрации тенанта автоматически стартует Trial."""
        tenant = make_tenant('trial-co', 'trial@test.com')
        sub = tenant.subscription
        assert sub.status == Subscription.STATUS_TRIAL
        assert sub.plan.slug == Plan.SLUG_BUSINESS

    def test_trial_lasts_14_days(self):
        """Trial длится ровно 14 дней."""
        tenant = make_tenant('trial14-co', 'trial14@test.com')
        sub = tenant.subscription
        delta = sub.current_period_end - sub.current_period_start
        assert delta.days == 14

    def test_upgrade_plan_changes_status_to_active(self):
        """После успешной оплаты статус подписки меняется на active."""
        tenant = make_tenant('upgrade-co', 'upgrade@test.com')
        BillingService.upgrade_plan(tenant, Plan.SLUG_PRO, Subscription.PERIOD_MONTHLY)
        tenant.subscription.refresh_from_db()
        assert tenant.subscription.status == Subscription.STATUS_ACTIVE
        assert tenant.subscription.plan.slug == Plan.SLUG_PRO

    def test_check_expired_trials(self):
        """Просроченные trial переходят в past_due."""
        tenant = make_tenant('expired-co', 'expired@test.com')
        sub = tenant.subscription
        # Принудительно протухаем дату окончания
        sub.current_period_end = date.today() - timedelta(days=1)
        sub.save()

        count = BillingService.check_expired_trials()
        assert count == 1
        sub.refresh_from_db()
        assert sub.status == Subscription.STATUS_PAST_DUE

    def test_grace_period_cancels_after_7_days(self):
        """Подписка отменяется после 7 дней grace period."""
        tenant = make_tenant('grace-co', 'grace@test.com')
        sub = tenant.subscription
        sub.status = Subscription.STATUS_PAST_DUE
        sub.current_period_end = date.today() - timedelta(days=8)
        sub.save()

        count = BillingService.check_grace_period_expired()
        assert count == 1
        sub.refresh_from_db()
        assert sub.status == Subscription.STATUS_CANCELLED


@pytest.mark.django_db
class TestLimitChecker:
    def test_starter_plan_blocks_at_1000_listings(self):
        """Starter план блокирует публикацию при 1000 активных объявлениях."""
        tenant = make_tenant('starter-co', 'starter@test.com')
        BillingService.upgrade_plan(tenant, Plan.SLUG_STARTER, Subscription.PERIOD_MONTHLY)
        tenant.active_listings_count = 1000
        tenant.save(update_fields=['active_listings_count'])

        can, reason = LimitChecker().can_publish(tenant)
        assert can is False
        assert '1000' in reason

    def test_can_publish_on_active_subscription(self):
        """Активная подписка разрешает публикацию."""
        tenant = make_tenant('active-co', 'active@test.com')
        can, _ = LimitChecker().can_publish(tenant)
        assert can is True

    def test_cancelled_subscription_blocks_publish(self):
        """Отменённая подписка блокирует публикацию."""
        tenant = make_tenant('cancelled-co', 'cancelled@test.com')
        sub = tenant.subscription
        sub.status = Subscription.STATUS_CANCELLED
        sub.save()

        can, reason = LimitChecker().can_publish(tenant)
        assert can is False
        assert 'неактивна' in reason

    def test_enterprise_no_limits(self):
        """Enterprise план не имеет лимитов."""
        tenant = make_tenant('enterprise-co', 'enterprise@test.com')
        BillingService.upgrade_plan(tenant, Plan.SLUG_ENTERPRISE, Subscription.PERIOD_MONTHLY)
        tenant.active_listings_count = 999999
        tenant.save(update_fields=['active_listings_count'])

        can, _ = LimitChecker().can_publish(tenant)
        assert can is True

    def test_grace_period_allows_existing_listings(self):
        """В grace period (past_due) существующие объявления остаются, но новые блокируются."""
        tenant = make_tenant('pastdue-co', 'pastdue@test.com')
        sub = tenant.subscription
        sub.status = Subscription.STATUS_PAST_DUE
        sub.save()

        can, reason = LimitChecker().can_publish(tenant)
        assert can is False
        assert 'неактивна' in reason

    def test_get_usage_summary_returns_correct_data(self):
        """get_usage_summary возвращает корректную структуру данных."""
        tenant = make_tenant('usage-co', 'usage@test.com')
        summary = LimitChecker().get_usage_summary(tenant)

        assert 'listings' in summary
        assert 'sku' in summary
        assert 'ai_credits' in summary
        assert summary['plan'] == Plan.SLUG_BUSINESS
