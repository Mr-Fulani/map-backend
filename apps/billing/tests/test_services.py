from datetime import date, timedelta

from django.utils import timezone

import pytest

from apps.billing.models import Plan, Subscription
from apps.billing.services import (
    BillingService, LimitChecker, add_billing_month, add_billing_year,
)
from apps.products.models import Product
from apps.tenants.services import TenantService


def make_tenant(slug, email):
    """Вспомогательная функция для создания тенанта с trial."""
    tenant, _ = TenantService.create_tenant(
        name=slug, slug=slug, owner_email=email, owner_password='pass12345',
    )
    return tenant


def test_add_billing_month_uses_last_day_when_next_month_is_shorter():
    assert add_billing_month(date(2026, 5, 31)) == date(2026, 6, 30)
    assert add_billing_month(date(2026, 1, 31)) == date(2026, 2, 28)
    assert add_billing_month(date(2026, 12, 31)) == date(2027, 1, 31)


def test_add_billing_year_handles_leap_day():
    assert add_billing_year(date(2026, 7, 29)) == date(2027, 7, 29)
    assert add_billing_year(date(2024, 2, 29)) == date(2025, 2, 28)


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

    def test_yearly_upgrade_creates_one_year_period(self):
        tenant = make_tenant('yearly-co', 'yearly@test.com')

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                'apps.billing.services.timezone.localdate',
                lambda: date(2024, 2, 29),
            )
            BillingService.upgrade_plan(
                tenant,
                Plan.SLUG_PRO,
                Subscription.PERIOD_YEARLY,
            )

        tenant.subscription.refresh_from_db()
        assert tenant.subscription.current_period_start == date(2024, 2, 29)
        assert tenant.subscription.current_period_end == date(2025, 2, 28)

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
        tenant.refresh_from_db()
        assert sub.status == Subscription.STATUS_PAST_DUE
        assert tenant.trial_ends_at is None

    def test_expired_trial_is_inactive_before_periodic_task_runs(self):
        """Дата немедленно ограничивает доступ, даже если status ещё trial."""
        tenant = make_tenant('expired-live-co', 'expired-live@test.com')
        sub = tenant.subscription
        sub.current_period_end = date.today() - timedelta(days=1)
        sub.save(update_fields=['current_period_end'])

        assert sub.status == Subscription.STATUS_TRIAL
        assert sub.effective_status == Subscription.STATUS_PAST_DUE
        assert sub.is_active is False
        assert sub.access_mode == Subscription.ACCESS_BILLING_ONLY

    def test_check_expired_paid_subscription(self):
        """Истёкший оплаченный период тоже переходит в past_due."""
        tenant = make_tenant('expired-paid-co', 'expired-paid@test.com')
        sub = tenant.subscription
        sub.status = Subscription.STATUS_ACTIVE
        sub.current_period_end = date.today() - timedelta(days=1)
        sub.save(update_fields=['status', 'current_period_end'])

        count = BillingService.check_expired_trials()

        assert count == 1
        sub.refresh_from_db()
        assert sub.status == Subscription.STATUS_PAST_DUE

    def test_extend_trial_syncs_legacy_tenant_date(self):
        tenant = make_tenant('extend-trial-co', 'extend-trial@test.com')
        sub = tenant.subscription
        sub.status = Subscription.STATUS_PAST_DUE
        sub.current_period_end = date.today() - timedelta(days=3)
        sub.save(update_fields=['status', 'current_period_end'])

        extended = BillingService.extend_trial(tenant, days=5)
        tenant.refresh_from_db()

        assert extended.status == Subscription.STATUS_TRIAL
        assert extended.current_period_end == date.today() + timedelta(days=5)
        assert timezone.localdate(tenant.trial_ends_at) == extended.current_period_end

    def test_extend_trial_does_not_downgrade_active_paid_subscription(self):
        tenant = make_tenant('no-downgrade-co', 'no-downgrade@test.com')
        sub = tenant.subscription
        sub.status = Subscription.STATUS_ACTIVE
        sub.save(update_fields=['status'])

        with pytest.raises(ValueError, match='платную подписку'):
            BillingService.extend_trial(tenant)

        sub.refresh_from_db()
        assert sub.status == Subscription.STATUS_ACTIVE

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

    def test_expired_trial_blocks_all_limit_checked_operations_immediately(self):
        tenant = make_tenant('expired-limits-co', 'expired-limits@test.com')
        sub = tenant.subscription
        sub.current_period_end = date.today() - timedelta(days=1)
        sub.save(update_fields=['current_period_end'])

        assert LimitChecker().can_publish(tenant)[0] is False
        assert LimitChecker().can_import_sku(tenant, count=0)[0] is False
        assert LimitChecker().can_generate_ai(tenant)[0] is False

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
        tenant.subscription.current_period_end = timezone.now().date() + timedelta(days=3)
        tenant.subscription.save(update_fields=['current_period_end'])
        other_tenant = make_tenant('other-usage-co', 'other-usage@test.com')
        Product.objects.create(
            tenant=tenant, article='SKU-001', name='Товар 1', price='100.00',
        )
        Product.objects.create(
            tenant=tenant, article='SKU-002', name='Товар 2', price='200.00',
        )
        Product.objects.create(
            tenant=other_tenant, article='SKU-OTHER', name='Чужой товар', price='300.00',
        )
        summary = LimitChecker().get_usage_summary(tenant)

        assert 'listings' in summary
        assert 'sku' in summary
        assert 'ai_credits' in summary
        assert tenant.sku_count == 0
        assert summary['sku']['used'] == 2
        assert summary['current_period_days_left'] == 3
        assert summary['plan'] == Plan.SLUG_BUSINESS

    def test_get_usage_summary_counts_active_listings_live(self):
        """listings.used считается вживую по статусу, без денормализованного поля."""
        from apps.datasources.encryption import encrypt
        from apps.marketplaces.models import Listing, MarketplaceAccount
        tenant = make_tenant('active-count-co', 'active-count@test.com')
        account = MarketplaceAccount.objects.create(
            tenant=tenant, name='Acc', external_id='999',
            credentials_enc=encrypt({'client_id': 'c', 'client_secret': 's'}),
        )
        product = Product.objects.create(
            tenant=tenant, article='A1', name='T', price='100.00',
        )
        Listing.objects.create(
            tenant=tenant, product=product, account=account,
            price_on_listing='100.00', status=Listing.STATUS_ACTIVE,
        )

        summary = LimitChecker().get_usage_summary(tenant)

        # денормализованное поле не трогали — оно 0, но в сводке живой подсчёт = 1
        assert tenant.active_listings_count == 0
        assert summary['listings']['used'] == 1

    def test_get_usage_summary_returns_days_left_for_active_subscription(self):
        """Остаток оплаченного периода доступен для active-подписки."""
        tenant = make_tenant('active-usage-co', 'active-usage@test.com')
        sub = tenant.subscription
        sub.status = Subscription.STATUS_ACTIVE
        sub.current_period_end = timezone.now().date() + timedelta(days=6)
        sub.save(update_fields=['status', 'current_period_end'])

        summary = LimitChecker().get_usage_summary(tenant)

        assert summary['subscription_status'] == Subscription.STATUS_ACTIVE
        assert summary['current_period_days_left'] == 6
