import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.billing.ai_wallet import AIWalletService
from apps.billing.models import AICreditPackage, Invoice, Plan, Subscription
from apps.billing.services import BillingService, GRACE_PERIOD_DAYS
from apps.billing.tasks import billing_check_expired
from apps.tenants.services import TenantService

# Официальный IP YooKassa для тестов
YOOKASSA_IP = '185.71.76.1'
NON_YOOKASSA_IP = '1.2.3.4'


def make_tenant_with_subscription(slug, plan_slug='business', status=Subscription.STATUS_TRIAL):
    """Создаёт тенанта с активной подпиской."""
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    sub = tenant.subscription
    if status != Subscription.STATUS_TRIAL:
        Subscription.objects.filter(pk=sub.pk).update(status=status)
        sub.refresh_from_db()
    return tenant, sub


@pytest.mark.django_db
class TestCheckoutAmounts:
    @pytest.mark.parametrize(
        ('plan_slug', 'expected_yearly'),
        [
            (Plan.SLUG_STARTER, Decimal('47040.00')),
            (Plan.SLUG_BUSINESS, Decimal('143040.00')),
            (Plan.SLUG_PRO, Decimal('335040.00')),
            (Plan.SLUG_ENTERPRISE, Decimal('767040.00')),
        ],
    )
    def test_seeded_yearly_price_is_full_twelve_month_payment(
        self,
        plan_slug,
        expected_yearly,
    ):
        plan = Plan.objects.get(slug=plan_slug)

        assert plan.price_yearly == expected_yearly
        assert plan.price_yearly_monthly_equivalent == (
            expected_yearly / Decimal('12')
        ).quantize(Decimal('0.01'))

    def test_monthly_checkout_charges_monthly_price(self):
        tenant, _sub = make_tenant_with_subscription('checkout-monthly')
        plan = Plan.objects.get(slug=Plan.SLUG_BUSINESS)

        with patch(
            'apps.billing.yookassa_client.create_payment',
            return_value=('pay_monthly', 'https://pay.example/monthly'),
        ) as create_payment:
            url = BillingService.create_payment(
                tenant,
                plan.slug,
                Subscription.PERIOD_MONTHLY,
                'https://app.example/return',
            )

        invoice = Invoice.objects.get(yookassa_payment_id='pay_monthly')
        assert url == 'https://pay.example/monthly'
        assert create_payment.call_args.kwargs['amount'] == plan.price_monthly
        assert invoice.amount == plan.price_monthly
        assert invoice.currency == 'RUB'

    def test_yearly_checkout_charges_full_yearly_price(self):
        tenant, _sub = make_tenant_with_subscription('checkout-yearly')
        plan = Plan.objects.get(slug=Plan.SLUG_BUSINESS)

        with patch(
            'apps.billing.yookassa_client.create_payment',
            return_value=('pay_yearly', 'https://pay.example/yearly'),
        ) as create_payment:
            BillingService.create_payment(
                tenant,
                plan.slug,
                Subscription.PERIOD_YEARLY,
                'https://app.example/return',
            )

        invoice = Invoice.objects.get(yookassa_payment_id='pay_yearly')
        assert create_payment.call_args.kwargs['amount'] == Decimal('143040.00')
        assert invoice.amount == Decimal('143040.00')
        assert invoice.metadata == {
            'plan_slug': Plan.SLUG_BUSINESS,
            'period': Subscription.PERIOD_YEARLY,
        }


@pytest.mark.django_db
class TestPaymentSucceeded:
    def test_payment_succeeded_activates_subscription(self):
        """Вебхук payment.succeeded → Invoice=paid, Subscription=active."""
        tenant, sub = make_tenant_with_subscription('yook-ok')
        invoice = Invoice.objects.create(
            tenant=tenant,
            amount=Decimal('990.00'),
            status=Invoice.STATUS_PENDING,
            yookassa_payment_id='pay_001',
        )

        with patch('apps.notifications.tasks.send_notification_task') as mock_notify:
            BillingService.handle_payment_success_webhook(
                payment_id='pay_001',
                amount='990.00',
                metadata={'plan_slug': 'business', 'period': 'monthly'},
            )

        invoice.refresh_from_db()
        sub.refresh_from_db()
        assert invoice.status == Invoice.STATUS_PAID
        assert invoice.paid_at is not None
        assert sub.status == Subscription.STATUS_ACTIVE
        mock_notify.delay.assert_called_once()

    def test_payment_succeeded_sends_billing_email(self):
        """Вебхук payment.succeeded → email-уведомление тенанту."""
        tenant, _ = make_tenant_with_subscription('yook-email')
        Invoice.objects.create(
            tenant=tenant,
            amount=Decimal('990.00'),
            status=Invoice.STATUS_PENDING,
            yookassa_payment_id='pay_002',
        )

        with patch('apps.notifications.tasks.send_notification_task') as mock_notify:
            BillingService.handle_payment_success_webhook(
                payment_id='pay_002',
                amount='990.00',
                metadata={'plan_slug': 'business', 'period': 'monthly'},
            )

        mock_notify.delay.assert_called_once()
        call_args = mock_notify.delay.call_args[0]
        assert call_args[0] == tenant.pk    # tenant_id
        assert call_args[1] == 'billing'    # level

    def test_payment_succeeded_requeues_limit_reached_listings(
        self, django_capture_on_commit_callbacks,
    ):
        """После активации подписки листинги «Лимит достигнут» уходят на перепубликацию."""
        tenant, _ = make_tenant_with_subscription('yook-requeue')
        Invoice.objects.create(
            tenant=tenant,
            amount=Decimal('990.00'),
            status=Invoice.STATUS_PENDING,
            yookassa_payment_id='pay_003',
        )

        with patch('apps.notifications.tasks.send_notification_task'), \
             patch('apps.marketplaces.tasks.requeue_limit_reached_listings') as mock_requeue:
            with django_capture_on_commit_callbacks(execute=True):
                BillingService.handle_payment_success_webhook(
                    payment_id='pay_003',
                    amount='990.00',
                    metadata={'plan_slug': 'business', 'period': 'monthly'},
                )

        mock_requeue.delay.assert_called_once_with(tenant.pk)

    def test_ai_topup_webhook_adds_purchased_credits_once(self):
        tenant, _sub = make_tenant_with_subscription('yook-ai-topup')
        package = AICreditPackage.objects.filter(is_active=True).first()
        invoice = Invoice.objects.create(
            tenant=tenant,
            amount=package.price_rub,
            status=Invoice.STATUS_PENDING,
            yookassa_payment_id='pay_ai_001',
            purchase_type=Invoice.TYPE_AI_TOPUP,
            metadata={'package_id': str(package.pk)},
        )
        before = AIWalletService.summary(tenant)['purchased']

        with patch('apps.notifications.tasks.send_notification_task'):
            BillingService.handle_payment_success_webhook(
                payment_id='pay_ai_001',
                amount=str(package.price_rub),
                metadata={},
            )
            BillingService.handle_payment_success_webhook(
                payment_id='pay_ai_001',
                amount=str(package.price_rub),
                metadata={},
            )

        invoice.refresh_from_db()
        after = AIWalletService.summary(tenant)['purchased']
        assert invoice.status == Invoice.STATUS_PAID
        assert after == before + package.credits

    @pytest.mark.parametrize(
        ('amount', 'currency'),
        [
            ('989.99', 'RUB'),
            ('990.00', 'USD'),
            ('invalid', 'RUB'),
        ],
    )
    def test_mismatched_amount_or_currency_does_not_activate_subscription(
        self,
        amount,
        currency,
    ):
        tenant, sub = make_tenant_with_subscription('yook-mismatch')
        invoice = Invoice.objects.create(
            tenant=tenant,
            amount=Decimal('990.00'),
            currency='RUB',
            status=Invoice.STATUS_PENDING,
            yookassa_payment_id='pay_mismatch',
            metadata={
                'plan_slug': Plan.SLUG_PRO,
                'period': Subscription.PERIOD_YEARLY,
            },
        )

        with patch('apps.notifications.tasks.send_notification_task') as notify:
            processed = BillingService.handle_payment_success_webhook(
                payment_id='pay_mismatch',
                amount=amount,
                currency=currency,
                metadata={},
            )

        invoice.refresh_from_db()
        sub.refresh_from_db()
        assert processed is False
        assert invoice.status == Invoice.STATUS_PENDING
        assert invoice.paid_at is None
        assert sub.status == Subscription.STATUS_TRIAL
        notify.delay.assert_not_called()

    def test_repeated_subscription_webhook_has_no_second_side_effect(self):
        tenant, sub = make_tenant_with_subscription('yook-idempotent')
        plan = Plan.objects.get(slug=Plan.SLUG_BUSINESS)
        invoice = Invoice.objects.create(
            tenant=tenant,
            amount=plan.price_yearly,
            currency='RUB',
            status=Invoice.STATUS_PENDING,
            yookassa_payment_id='pay_idempotent',
            metadata={
                'plan_slug': plan.slug,
                'period': Subscription.PERIOD_YEARLY,
            },
        )

        with patch('apps.notifications.tasks.send_notification_task') as notify:
            BillingService.handle_payment_success_webhook(
                'pay_idempotent',
                str(plan.price_yearly),
                {},
                currency='RUB',
            )
            sub.refresh_from_db()
            first_period_end = sub.current_period_end
            BillingService.handle_payment_success_webhook(
                'pay_idempotent',
                str(plan.price_yearly),
                {'plan_slug': Plan.SLUG_ENTERPRISE},
                currency='RUB',
            )

        invoice.refresh_from_db()
        sub.refresh_from_db()
        assert invoice.status == Invoice.STATUS_PAID
        assert sub.current_period_end == first_period_end
        assert sub.plan_id == plan.pk
        notify.delay.assert_called_once()

    def test_stored_invoice_metadata_wins_over_webhook_metadata(self):
        tenant, _sub = make_tenant_with_subscription('yook-metadata')
        plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
        Invoice.objects.create(
            tenant=tenant,
            amount=plan.price_monthly,
            currency='RUB',
            status=Invoice.STATUS_PENDING,
            yookassa_payment_id='pay_metadata',
            metadata={
                'plan_slug': plan.slug,
                'period': Subscription.PERIOD_MONTHLY,
            },
        )

        with patch('apps.notifications.tasks.send_notification_task'):
            BillingService.handle_payment_success_webhook(
                'pay_metadata',
                str(plan.price_monthly),
                {
                    'plan_slug': Plan.SLUG_ENTERPRISE,
                    'period': Subscription.PERIOD_YEARLY,
                },
                currency='RUB',
            )

        tenant.subscription.refresh_from_db()
        assert tenant.subscription.plan_id == plan.pk
        assert tenant.subscription.billing_period == Subscription.PERIOD_MONTHLY


@pytest.mark.django_db
class TestPaymentFailed:
    def test_payment_failed_sets_invoice_failed(self):
        """Вебхук payment.canceled → Invoice=failed."""
        tenant, _ = make_tenant_with_subscription('yook-fail')
        invoice = Invoice.objects.create(
            tenant=tenant,
            amount=Decimal('990.00'),
            status=Invoice.STATUS_PENDING,
            yookassa_payment_id='pay_fail_001',
        )

        BillingService.handle_payment_failed_webhook('pay_fail_001')

        invoice.refresh_from_db()
        assert invoice.status == Invoice.STATUS_FAILED

    def test_billing_check_expired_sets_past_due(self):
        """billing_check_expired переводит истёкший trial в past_due."""
        tenant, sub = make_tenant_with_subscription('yook-past-due')
        Subscription.objects.filter(pk=sub.pk).update(
            current_period_end=date.today() - timedelta(days=1),
        )

        with patch('apps.notifications.tasks.send_notification_task'):
            result = billing_check_expired()

        sub.refresh_from_db()
        assert sub.status == Subscription.STATUS_PAST_DUE
        assert result['past_due_updated'] >= 1


@pytest.mark.django_db
class TestGracePeriod:
    def test_grace_period_allows_existing_listings(self):
        """В течение grace period LimitChecker.can_publish возвращает False для новых, но статус past_due."""
        from apps.billing.services import LimitChecker

        tenant, sub = make_tenant_with_subscription('yook-grace', status=Subscription.STATUS_PAST_DUE)
        can, reason = LimitChecker().can_publish(tenant)
        # past_due не является is_active, поэтому новые публикации заблокированы
        assert can is False

    def test_grace_period_expired_cancels_subscription(self):
        """Через 7 дней past_due → subscription становится cancelled."""
        tenant, sub = make_tenant_with_subscription('yook-cancel', status=Subscription.STATUS_PAST_DUE)
        past_due_start = date.today() - timedelta(days=GRACE_PERIOD_DAYS + 1)
        Subscription.objects.filter(pk=sub.pk).update(current_period_end=past_due_start)

        with patch('apps.notifications.tasks.send_notification_task'):
            result = billing_check_expired()

        sub.refresh_from_db()
        assert sub.status == Subscription.STATUS_CANCELLED
        assert result['cancelled'] >= 1


@pytest.mark.django_db
class TestWebhookSecurity:
    def test_invalid_ip_returns_400(self, client):
        """Запрос с неизвестного IP → 400."""
        payload = json.dumps({
            'event': 'payment.succeeded',
            'object': {'id': 'pay_x', 'amount': {'value': '100'}, 'metadata': {}},
        })
        resp = client.post(
            '/api/v1/billing/webhook/yookassa/',
            data=payload,
            content_type='application/json',
            REMOTE_ADDR=NON_YOOKASSA_IP,
        )
        assert resp.status_code == 400

    def test_valid_yookassa_ip_accepted(self, client):
        """Запрос с IP YooKassa → 200 (неизвестный payment_id — просто игнорируется)."""
        payload = json.dumps({
            'event': 'payment.canceled',
            'object': {'id': 'pay_nonexistent', 'amount': {'value': '100'}, 'metadata': {}},
        })
        resp = client.post(
            '/api/v1/billing/webhook/yookassa/',
            data=payload,
            content_type='application/json',
            REMOTE_ADDR=YOOKASSA_IP,
        )
        assert resp.status_code == 200

    def test_webhook_with_wrong_currency_is_acknowledged_but_not_applied(self, client):
        tenant, sub = make_tenant_with_subscription('webhook-currency')
        invoice = Invoice.objects.create(
            tenant=tenant,
            amount=Decimal('990.00'),
            currency='RUB',
            status=Invoice.STATUS_PENDING,
            yookassa_payment_id='pay_wrong_currency',
        )
        payload = json.dumps({
            'event': 'payment.succeeded',
            'object': {
                'id': 'pay_wrong_currency',
                'amount': {'value': '990.00', 'currency': 'USD'},
                'metadata': {},
            },
        })

        response = client.post(
            '/api/v1/billing/webhook/yookassa/',
            data=payload,
            content_type='application/json',
            REMOTE_ADDR=YOOKASSA_IP,
        )

        invoice.refresh_from_db()
        sub.refresh_from_db()
        assert response.status_code == 200
        assert invoice.status == Invoice.STATUS_PENDING
        assert sub.status == Subscription.STATUS_TRIAL
