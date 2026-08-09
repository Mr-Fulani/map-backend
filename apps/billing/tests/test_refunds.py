import json
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.billing.ai_wallet import AIWalletService
from apps.billing.models import (
    AICreditPackage, AICreditTransaction, BillingWebhookEvent,
    Invoice, PaymentReversal,
)
from apps.billing.services import BillingService
from apps.billing.yookassa_client import (
    PaymentSnapshot, RefundSnapshot,
)
from apps.tenants.services import TenantService


YOOKASSA_IP = '185.71.76.1'


def make_paid_topup(slug):
    tenant, _ = TenantService.create_tenant(
        slug,
        slug,
        f'{slug}@test.com',
        'pass12345',
    )
    package = AICreditPackage.objects.filter(is_active=True).order_by('sort_order').first()
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=package.price_rub,
        currency='RUB',
        purchase_type=Invoice.TYPE_AI_TOPUP,
        metadata={'package_id': str(package.pk)},
        status=Invoice.STATUS_PAID,
        yookassa_payment_id=f'pay_{slug}',
    )
    AIWalletService.topup(
        tenant,
        package.credits,
        idempotency_key=f'topup:{slug}',
        reference=invoice.yookassa_payment_id,
    )
    return tenant, package, invoice


@pytest.mark.django_db
def test_full_topup_refund_reverses_unused_credits():
    tenant, package, invoice = make_paid_topup('refund-full')

    reversal = BillingService.handle_reversal_success(
        invoice_id=invoice.pk,
        provider_reference='refund_full_1',
        payment_id=invoice.yookassa_payment_id,
        amount=invoice.amount,
        currency='RUB',
    )

    invoice.refresh_from_db()
    summary = AIWalletService.summary(tenant)
    assert reversal.status == PaymentReversal.STATUS_APPLIED
    assert reversal.credits_requested == package.credits
    assert reversal.credits_reversed == package.credits
    assert reversal.credit_shortfall == 0
    assert summary['purchased'] == 0
    assert invoice.status == Invoice.STATUS_REFUNDED
    assert invoice.refunded_amount == invoice.amount


@pytest.mark.django_db
def test_two_partial_refunds_reverse_exact_package_total():
    tenant, package, invoice = make_paid_topup('refund-partial')
    half = invoice.amount / 2

    first = BillingService.handle_reversal_success(
        invoice_id=invoice.pk,
        provider_reference='refund_partial_1',
        payment_id=invoice.yookassa_payment_id,
        amount=half,
        currency='RUB',
    )
    invoice.refresh_from_db()
    assert first.credits_reversed == package.credits / 2
    assert invoice.status == Invoice.STATUS_PARTIALLY_REFUNDED

    second = BillingService.handle_reversal_success(
        invoice_id=invoice.pk,
        provider_reference='refund_partial_2',
        payment_id=invoice.yookassa_payment_id,
        amount=half,
        currency='RUB',
    )

    invoice.refresh_from_db()
    assert second.credits_reversed == package.credits / 2
    assert AIWalletService.summary(tenant)['purchased'] == 0
    assert invoice.status == Invoice.STATUS_REFUNDED


@pytest.mark.django_db
def test_repeated_refund_reference_is_idempotent():
    tenant, package, invoice = make_paid_topup('refund-idempotent')

    first = BillingService.handle_reversal_success(
        invoice_id=invoice.pk,
        provider_reference='refund_same',
        payment_id=invoice.yookassa_payment_id,
        amount=invoice.amount,
        currency='RUB',
    )
    second = BillingService.handle_reversal_success(
        invoice_id=invoice.pk,
        provider_reference='refund_same',
        payment_id=invoice.yookassa_payment_id,
        amount=invoice.amount,
        currency='RUB',
    )

    assert second.pk == first.pk
    assert PaymentReversal.objects.filter(provider_reference='refund_same').count() == 1
    assert AIWalletService.summary(tenant)['purchased'] == 0
    assert AICreditTransaction.objects.filter(
        tenant=tenant,
        kind=AICreditTransaction.KIND_REFUND,
        reference='refund_same',
    ).count() == 1


@pytest.mark.django_db
def test_spent_credits_create_manual_review_without_negative_balance():
    tenant, package, invoice = make_paid_topup('refund-spent')
    wallet = AIWalletService.ensure_wallet(tenant)
    wallet.included_balance = 0
    wallet.save(update_fields=['included_balance'])
    reservation = AIWalletService.reserve(
        tenant,
        Decimal('600'),
        key='refund-spent:charge',
    )
    with patch('apps.notifications.tasks.send_notification_task.delay'):
        AIWalletService.settle(tenant, reservation, Decimal('600'))

    reversal = BillingService.handle_reversal_success(
        invoice_id=invoice.pk,
        provider_reference='refund_spent_1',
        payment_id=invoice.yookassa_payment_id,
        amount=invoice.amount,
        currency='RUB',
    )

    invoice.refresh_from_db()
    summary = AIWalletService.summary(tenant)
    assert reversal.status == PaymentReversal.STATUS_MANUAL_REVIEW
    assert reversal.credits_requested == package.credits
    assert reversal.credits_reversed == Decimal('400')
    assert reversal.credit_shortfall == Decimal('600')
    assert summary['purchased'] == 0
    assert summary['available'] >= 0
    assert invoice.status == Invoice.STATUS_MANUAL_REVIEW
    assert invoice.refund_review_required is True


@pytest.mark.django_db
def test_refund_does_not_revoke_credits_reserved_for_running_request():
    tenant, package, invoice = make_paid_topup('refund-reserved')
    wallet = AIWalletService.ensure_wallet(tenant)
    wallet.included_balance = 0
    wallet.save(update_fields=['included_balance'])
    AIWalletService.reserve(
        tenant,
        Decimal('600'),
        key='refund-reserved:request',
    )

    reversal = BillingService.handle_reversal_success(
        invoice_id=invoice.pk,
        provider_reference='refund_reserved_1',
        payment_id=invoice.yookassa_payment_id,
        amount=invoice.amount,
        currency='RUB',
    )

    summary = AIWalletService.summary(tenant)
    assert reversal.credits_reversed == package.credits - Decimal('600')
    assert reversal.credit_shortfall == Decimal('600')
    assert summary['purchased'] == Decimal('600')
    assert summary['reserved'] == Decimal('600')
    assert summary['available'] == 0


@pytest.mark.django_db
def test_subscription_refund_is_sent_to_manual_review():
    tenant, _package, _topup_invoice = make_paid_topup('refund-subscription')
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('14900'),
        currency='RUB',
        purchase_type=Invoice.TYPE_SUBSCRIPTION,
        status=Invoice.STATUS_PAID,
        yookassa_payment_id='pay_subscription_refund',
    )

    reversal = BillingService.handle_reversal_success(
        invoice_id=invoice.pk,
        provider_reference='refund_subscription_1',
        payment_id=invoice.yookassa_payment_id,
        amount=invoice.amount,
        currency='RUB',
    )

    invoice.refresh_from_db()
    assert reversal.status == PaymentReversal.STATUS_MANUAL_REVIEW
    assert reversal.credits_reversed == 0
    assert invoice.status == Invoice.STATUS_MANUAL_REVIEW
    assert invoice.refund_review_required is True


@pytest.mark.django_db
def test_chargeback_uses_same_auditable_reversal_path():
    tenant, package, invoice = make_paid_topup('chargeback')

    reversal = BillingService.record_chargeback(
        invoice,
        invoice.amount,
        external_reference='chargeback_1',
    )

    assert reversal.kind == PaymentReversal.KIND_CHARGEBACK
    assert reversal.credits_reversed == package.credits
    assert AICreditTransaction.objects.filter(
        tenant=tenant,
        kind=AICreditTransaction.KIND_CHARGEBACK,
        reference='chargeback_1',
    ).exists()


@pytest.mark.django_db
def test_refund_webhook_is_logged_and_duplicate_delivery_is_counted(client):
    tenant, package, invoice = make_paid_topup('refund-webhook')
    payload = {
        'type': 'notification',
        'event': 'refund.succeeded',
        'object': {
            'id': 'refund_webhook_1',
            'status': 'succeeded',
            'payment_id': invoice.yookassa_payment_id,
            'amount': {'value': str(invoice.amount), 'currency': 'RUB'},
        },
    }

    with patch(
        'apps.billing.webhook_processing.fetch_refund',
        return_value=RefundSnapshot(
            id='refund_webhook_1',
            status='succeeded',
            payment_id=invoice.yookassa_payment_id,
            amount=invoice.amount,
            currency='RUB',
        ),
    ) as fetch_refund, patch(
        'apps.billing.webhook_processing.fetch_payment',
        return_value=PaymentSnapshot(
            id=invoice.yookassa_payment_id,
            status='succeeded',
            amount=invoice.amount,
            currency='RUB',
        ),
    ):
        first = client.post(
            '/api/v1/billing/webhook/yookassa/',
            data=json.dumps(payload),
            content_type='application/json',
            REMOTE_ADDR=YOOKASSA_IP,
        )
        second = client.post(
            '/api/v1/billing/webhook/yookassa/',
            data=json.dumps(payload),
            content_type='application/json',
            REMOTE_ADDR=YOOKASSA_IP,
        )

    event = BillingWebhookEvent.objects.get(
        idempotency_key='refund.succeeded:refund_webhook_1',
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert event.decision == BillingWebhookEvent.DECISION_APPLIED
    assert event.delivery_count == 2
    assert event.invoice_id == invoice.pk
    assert event.tenant_id == tenant.pk
    assert PaymentReversal.objects.filter(invoice=invoice).count() == 1
    assert AIWalletService.summary(tenant)['purchased'] == 0
    fetch_refund.assert_called_once_with('refund_webhook_1')


@pytest.mark.django_db
def test_webhook_processing_error_is_retried_on_next_delivery(client):
    tenant, _package, invoice = make_paid_topup('webhook-retry')
    payload = {
        'event': 'payment.canceled',
        'object': {
            'id': invoice.yookassa_payment_id,
            'status': 'canceled',
        },
    }

    authoritative_payment = PaymentSnapshot(
        id=invoice.yookassa_payment_id,
        status='canceled',
        amount=invoice.amount,
        currency='RUB',
    )
    with patch(
        'apps.billing.webhook_processing.fetch_payment',
        return_value=authoritative_payment,
    ), patch(
        'apps.billing.services.BillingService.handle_payment_failed_webhook',
        side_effect=RuntimeError('temporary failure'),
    ):
        first = client.post(
            '/api/v1/billing/webhook/yookassa/',
            data=json.dumps(payload),
            content_type='application/json',
            REMOTE_ADDR=YOOKASSA_IP,
        )

    event = BillingWebhookEvent.objects.get(
        idempotency_key=f'payment.canceled:{invoice.yookassa_payment_id}',
    )
    assert first.status_code == 503
    assert first['Retry-After']
    assert event.decision == BillingWebhookEvent.DECISION_ERROR

    invoice.status = Invoice.STATUS_PENDING
    invoice.save(update_fields=['status'])
    with patch(
        'apps.billing.webhook_processing.fetch_payment',
        return_value=authoritative_payment,
    ):
        second = client.post(
            '/api/v1/billing/webhook/yookassa/',
            data=json.dumps(payload),
            content_type='application/json',
            REMOTE_ADDR=YOOKASSA_IP,
        )

    event.refresh_from_db()
    invoice.refresh_from_db()
    assert second.status_code == 200
    assert event.delivery_count == 2
    assert event.decision == BillingWebhookEvent.DECISION_APPLIED
    assert invoice.status == Invoice.STATUS_FAILED


@pytest.mark.django_db
def test_terminal_authoritative_status_mismatch_is_ignored_without_side_effect(client):
    _tenant, _package, invoice = make_paid_topup('webhook-status-mismatch')
    payload = {
        'event': 'refund.succeeded',
        'object': {
            'id': 'refund_wrong_status',
            'status': 'canceled',
            'payment_id': invoice.yookassa_payment_id,
            'amount': {'value': str(invoice.amount), 'currency': 'RUB'},
        },
    }

    with patch(
        'apps.billing.webhook_processing.fetch_refund',
        return_value=RefundSnapshot(
            id='refund_wrong_status',
            status='canceled',
            payment_id=invoice.yookassa_payment_id,
            amount=invoice.amount,
            currency='RUB',
        ),
    ), patch(
        'apps.billing.webhook_processing.fetch_payment',
        return_value=PaymentSnapshot(
            id=invoice.yookassa_payment_id,
            status='succeeded',
            amount=invoice.amount,
            currency='RUB',
        ),
    ):
        response = client.post(
            '/api/v1/billing/webhook/yookassa/',
            data=json.dumps(payload),
            content_type='application/json',
            REMOTE_ADDR=YOOKASSA_IP,
        )

    event = BillingWebhookEvent.objects.get(object_id='refund_wrong_status')
    invoice.refresh_from_db()
    assert response.status_code == 200
    assert event.decision == BillingWebhookEvent.DECISION_IGNORED
    assert PaymentReversal.objects.filter(invoice=invoice).count() == 0
    assert invoice.status == Invoice.STATUS_PAID


@pytest.mark.django_db
def test_invalid_ip_attempt_is_logged_without_payment_method_data(client):
    payload = {
        'type': 'notification',
        'event': 'payment.succeeded',
        'object': {
            'id': 'pay_spoofed',
            'status': 'succeeded',
            'amount': {'value': '990.00', 'currency': 'RUB'},
            'payment_method': {'card': {'first6': '555555', 'last4': '4444'}},
        },
    }

    response = client.post(
        '/api/v1/billing/webhook/yookassa/',
        data=json.dumps(payload),
        content_type='application/json',
        REMOTE_ADDR='1.2.3.4',
    )

    event = BillingWebhookEvent.objects.get(object_id='pay_spoofed')
    assert response.status_code == 400
    assert event.decision == BillingWebhookEvent.DECISION_REJECTED
    assert 'payment_method' not in event.payload['object']


@pytest.mark.django_db
def test_spoofed_leftmost_forwarded_ip_is_rejected(client):
    payload = {
        'event': 'payment.canceled',
        'object': {'id': 'pay_forwarded_spoof', 'status': 'canceled'},
    }

    response = client.post(
        '/api/v1/billing/webhook/yookassa/',
        data=json.dumps(payload),
        content_type='application/json',
        REMOTE_ADDR='127.0.0.1',
        HTTP_X_FORWARDED_FOR=f'{YOOKASSA_IP}, 1.2.3.4',
    )

    assert response.status_code == 400
    assert BillingWebhookEvent.objects.get(
        object_id='pay_forwarded_spoof',
    ).source_ip == '1.2.3.4'


@pytest.mark.django_db
def test_payment_canceled_does_not_downgrade_paid_invoice():
    _tenant, _package, invoice = make_paid_topup('cancel-after-paid')

    BillingService.handle_payment_failed_webhook(
        invoice.pk,
        payment_id=invoice.yookassa_payment_id,
    )

    invoice.refresh_from_db()
    assert invoice.status == Invoice.STATUS_PAID
