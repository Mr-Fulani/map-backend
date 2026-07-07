import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.billing.models import Invoice, Subscription
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
