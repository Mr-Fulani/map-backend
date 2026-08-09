import json
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.throttling import ScopedRateThrottle

from apps.billing.ai_wallet import AIWalletService
from apps.billing.models import (
    AICreditPackage, BillingOutboxEvent, BillingWebhookEvent, Invoice,
    PaymentReversal, Plan, Subscription,
)
from apps.billing.services import BillingService
from apps.billing.views import (
    AITopupCheckoutView, CheckoutView, _finalize_webhook_event,
)
from apps.billing.yookassa_client import (
    PaymentSnapshot, RefundSnapshot, YooKassaAPIError,
)
from apps.tenants.services import TenantService
from apps.tenants.tests.auth import owner_client


YOOKASSA_IP = '185.71.76.1'
WEBHOOK_URL = '/api/v1/billing/webhook/yookassa/'


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(
        slug,
        slug,
        f'{slug}@test.com',
        'pass12345',
    )
    return tenant


def payment_payload(payment_id, event='payment.succeeded'):
    return {
        'event': event,
        'object': {
            'id': payment_id,
            'status': 'forged-status',
            'amount': {'value': '0.01', 'currency': 'USD'},
            'metadata': {
                'plan_slug': Plan.SLUG_ENTERPRISE,
                'period': Subscription.PERIOD_YEARLY,
            },
        },
    }


@pytest.mark.django_db
def test_current_official_yookassa_ip_range_is_accepted(client):
    response = client.post(
        WEBHOOK_URL,
        data=json.dumps(payment_payload('pay_official_range', 'payment.pending')),
        content_type='application/json',
        REMOTE_ADDR='77.75.154.129',
    )

    assert response.status_code == 200
    assert BillingWebhookEvent.objects.get(
        object_id='pay_official_range',
    ).decision == BillingWebhookEvent.DECISION_IGNORED


@pytest.mark.django_db
def test_webhook_uses_provider_snapshot_and_invoice_metadata_only(client):
    tenant = make_tenant('authoritative-webhook')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=plan.price_monthly,
        currency='RUB',
        status=Invoice.STATUS_PENDING,
        yookassa_payment_id='pay_authoritative',
        metadata={
            'plan_slug': plan.slug,
            'period': Subscription.PERIOD_MONTHLY,
        },
    )
    snapshot = PaymentSnapshot(
        id=invoice.yookassa_payment_id,
        status='succeeded',
        amount=invoice.amount,
        currency='RUB',
    )

    with patch('apps.billing.webhook_processing.fetch_payment', return_value=snapshot), \
         patch('apps.notifications.tasks.send_notification_task'):
        response = client.post(
            WEBHOOK_URL,
            data=json.dumps(payment_payload(invoice.yookassa_payment_id)),
            content_type='application/json',
            REMOTE_ADDR=YOOKASSA_IP,
        )

    invoice.refresh_from_db()
    tenant.subscription.refresh_from_db()
    event = BillingWebhookEvent.objects.get(object_id=invoice.yookassa_payment_id)
    assert response.status_code == 200
    assert invoice.status == Invoice.STATUS_PAID
    assert tenant.subscription.plan_id == plan.pk
    assert tenant.subscription.billing_period == Subscription.PERIOD_MONTHLY
    assert event.amount == invoice.amount
    assert event.currency == 'RUB'
    assert 'metadata' not in event.payload['object']


@pytest.mark.django_db
def test_provider_failure_is_retryable_and_has_no_business_side_effect(client):
    tenant = make_tenant('provider-failure')
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('100.00'),
        status=Invoice.STATUS_PENDING,
        yookassa_payment_id='pay_provider_failure',
    )

    with patch(
        'apps.billing.webhook_processing.fetch_payment',
        side_effect=YooKassaAPIError('temporary'),
    ), patch.object(
        BillingService,
        'handle_payment_success_webhook',
    ) as apply_payment:
        response = client.post(
            WEBHOOK_URL,
            data=json.dumps(payment_payload(invoice.yookassa_payment_id)),
            content_type='application/json',
            REMOTE_ADDR=YOOKASSA_IP,
        )

    invoice.refresh_from_db()
    event = BillingWebhookEvent.objects.get(object_id=invoice.yookassa_payment_id)
    assert response.status_code == 503
    assert response['Retry-After']
    assert event.decision == BillingWebhookEvent.DECISION_ERROR
    assert invoice.status == Invoice.STATUS_PENDING
    apply_payment.assert_not_called()


@pytest.mark.django_db
def test_test_payment_never_activates_production_entitlements(client, settings):
    settings.YOOKASSA_ALLOW_TEST_PAYMENTS = False
    tenant = make_tenant('test-payment-rejected')
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('100.00'),
        status=Invoice.STATUS_PENDING,
        yookassa_payment_id='pay_test_rejected',
    )
    snapshot = PaymentSnapshot(
        id=invoice.yookassa_payment_id,
        status='succeeded',
        amount=invoice.amount,
        currency='RUB',
        test=True,
    )

    with patch(
        'apps.billing.webhook_processing.fetch_payment',
        return_value=snapshot,
    ), patch.object(
        BillingService,
        'handle_payment_success_webhook',
    ) as apply_payment:
        response = client.post(
            WEBHOOK_URL,
            data=json.dumps(payment_payload(invoice.yookassa_payment_id)),
            content_type='application/json',
            REMOTE_ADDR=YOOKASSA_IP,
        )

    invoice.refresh_from_db()
    event = BillingWebhookEvent.objects.get(object_id=invoice.yookassa_payment_id)
    assert response.status_code == 200
    assert invoice.status == Invoice.STATUS_MANUAL_REVIEW
    assert event.decision == BillingWebhookEvent.DECISION_MANUAL_REVIEW
    assert 'Тестовый платёж' in event.reason
    apply_payment.assert_not_called()


@pytest.mark.django_db
def test_refund_ignores_spoofed_cross_tenant_payment_id(client):
    first_tenant = make_tenant('refund-authoritative-first')
    second_tenant = make_tenant('refund-authoritative-second')
    package = AICreditPackage.objects.filter(is_active=True).first()
    first_invoice = Invoice.objects.create(
        tenant=first_tenant,
        amount=package.price_rub,
        currency='RUB',
        purchase_type=Invoice.TYPE_AI_TOPUP,
        metadata={'package_id': str(package.pk)},
        status=Invoice.STATUS_PAID,
        yookassa_payment_id='pay_refund_authoritative_first',
    )
    second_invoice = Invoice.objects.create(
        tenant=second_tenant,
        amount=package.price_rub,
        currency='RUB',
        purchase_type=Invoice.TYPE_AI_TOPUP,
        metadata={'package_id': str(package.pk)},
        status=Invoice.STATUS_PAID,
        yookassa_payment_id='pay_refund_authoritative_second',
    )
    for tenant, invoice in (
        (first_tenant, first_invoice),
        (second_tenant, second_invoice),
    ):
        AIWalletService.topup(
            tenant,
            package.credits,
            idempotency_key=f'topup:{invoice.pk}',
            reference=invoice.yookassa_payment_id,
        )

    payload = {
        'event': 'refund.succeeded',
        'object': {
            'id': 'refund_cross_tenant',
            'status': 'succeeded',
            'payment_id': second_invoice.yookassa_payment_id,
            'amount': {'value': str(second_invoice.amount), 'currency': 'RUB'},
        },
    }
    with patch(
        'apps.billing.webhook_processing.fetch_refund',
        return_value=RefundSnapshot(
            id='refund_cross_tenant',
            status='succeeded',
            payment_id=first_invoice.yookassa_payment_id,
            amount=first_invoice.amount,
            currency='RUB',
        ),
    ), patch(
        'apps.billing.webhook_processing.fetch_payment',
        return_value=PaymentSnapshot(
            id=first_invoice.yookassa_payment_id,
            status='succeeded',
            amount=first_invoice.amount,
            currency='RUB',
        ),
    ):
        response = client.post(
            WEBHOOK_URL,
            data=json.dumps(payload),
            content_type='application/json',
            REMOTE_ADDR=YOOKASSA_IP,
        )

    first_invoice.refresh_from_db()
    second_invoice.refresh_from_db()
    event = BillingWebhookEvent.objects.get(object_id='refund_cross_tenant')
    assert response.status_code == 200
    assert event.invoice_id == first_invoice.pk
    assert event.tenant_id == first_tenant.pk
    assert first_invoice.status == Invoice.STATUS_REFUNDED
    assert second_invoice.status == Invoice.STATUS_PAID
    assert AIWalletService.summary(first_tenant)['purchased'] == 0
    assert AIWalletService.summary(second_tenant)['purchased'] == package.credits


@pytest.mark.django_db
def test_refund_terminal_linked_payment_contradiction_is_finally_ignored(client):
    tenant = make_tenant('refund-linked-payment-state')
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('100.00'),
        currency='RUB',
        status=Invoice.STATUS_PAID,
        yookassa_payment_id='pay_refund_linked_canceled',
    )
    payload = {
        'event': 'refund.succeeded',
        'object': {'id': 'refund_linked_canceled'},
    }
    with patch(
        'apps.billing.webhook_processing.fetch_refund',
        return_value=RefundSnapshot(
            id='refund_linked_canceled',
            status='succeeded',
            payment_id=invoice.yookassa_payment_id,
            amount=Decimal('10.00'),
            currency='RUB',
        ),
    ), patch(
        'apps.billing.webhook_processing.fetch_payment',
        return_value=PaymentSnapshot(
            id=invoice.yookassa_payment_id,
            status='canceled',
            amount=invoice.amount,
            currency='RUB',
            test=False,
        ),
    ):
        response = client.post(
            WEBHOOK_URL,
            data=json.dumps(payload),
            content_type='application/json',
            REMOTE_ADDR=YOOKASSA_IP,
        )

    event = BillingWebhookEvent.objects.get(object_id='refund_linked_canceled')
    invoice.refresh_from_db()
    assert response.status_code == 200
    assert event.decision == BillingWebhookEvent.DECISION_IGNORED
    assert invoice.status == Invoice.STATUS_PAID
    assert not PaymentReversal.objects.filter(invoice=invoice).exists()


@pytest.mark.django_db
def test_refund_waits_for_transient_linked_payment_state(client):
    tenant = make_tenant('refund-linked-payment-pending')
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('100.00'),
        currency='RUB',
        status=Invoice.STATUS_PAID,
        yookassa_payment_id='pay_refund_linked_pending',
    )
    with patch(
        'apps.billing.webhook_processing.fetch_refund',
        return_value=RefundSnapshot(
            id='refund_linked_pending',
            status='succeeded',
            payment_id=invoice.yookassa_payment_id,
            amount=Decimal('10.00'),
            currency='RUB',
        ),
    ), patch(
        'apps.billing.webhook_processing.fetch_payment',
        return_value=PaymentSnapshot(
            id=invoice.yookassa_payment_id,
            status='pending',
            amount=invoice.amount,
            currency='RUB',
            test=False,
        ),
    ):
        response = client.post(
            WEBHOOK_URL,
            data=json.dumps({
                'event': 'refund.succeeded',
                'object': {'id': 'refund_linked_pending'},
            }),
            content_type='application/json',
            REMOTE_ADDR=YOOKASSA_IP,
        )

    event = BillingWebhookEvent.objects.get(object_id='refund_linked_pending')
    assert response.status_code == 503
    assert event.decision == BillingWebhookEvent.DECISION_ERROR
    assert event.next_reconciliation_at is not None


@pytest.mark.django_db
def test_invalid_supported_id_is_final_rejection_without_provider_call(client):
    payload = payment_payload('../not-a-provider-id')
    with patch('apps.billing.webhook_processing.fetch_payment') as fetch_payment:
        response = client.post(
            WEBHOOK_URL,
            data=json.dumps(payload),
            content_type='application/json',
            REMOTE_ADDR=YOOKASSA_IP,
        )

    event = BillingWebhookEvent.objects.get(event_type='payment.succeeded')
    assert response.status_code == 200
    assert event.decision == BillingWebhookEvent.DECISION_REJECTED
    fetch_payment.assert_not_called()


@pytest.mark.django_db
def test_unsupported_event_is_ignored_without_provider_call(client):
    payload = payment_payload('pay_unsupported', event='payment.waiting_for_capture')
    with patch('apps.billing.webhook_processing.fetch_payment') as fetch_payment, \
         patch('apps.billing.webhook_processing.fetch_refund') as fetch_refund:
        response = client.post(
            WEBHOOK_URL,
            data=json.dumps(payload),
            content_type='application/json',
            REMOTE_ADDR=YOOKASSA_IP,
        )

    event = BillingWebhookEvent.objects.get(object_id='pay_unsupported')
    assert response.status_code == 200
    assert event.decision == BillingWebhookEvent.DECISION_IGNORED
    fetch_payment.assert_not_called()
    fetch_refund.assert_not_called()


@pytest.mark.django_db
def test_parallel_delivery_does_not_start_second_provider_fetch(client):
    payment_id = 'pay_already_processing'
    BillingWebhookEvent.objects.create(
        event_type='payment.succeeded',
        object_id=payment_id,
        idempotency_key=f'payment.succeeded:{payment_id}',
        decision=BillingWebhookEvent.DECISION_RECEIVED,
        processing_token=uuid.uuid4(),
        processing_started_at=timezone.now(),
    )

    with patch('apps.billing.webhook_processing.fetch_payment') as fetch_payment:
        response = client.post(
            WEBHOOK_URL,
            data=json.dumps(payment_payload(payment_id)),
            content_type='application/json',
            REMOTE_ADDR=YOOKASSA_IP,
        )

    event = BillingWebhookEvent.objects.get(object_id=payment_id)
    assert response.status_code == 503
    assert event.delivery_count == 2
    assert event.decision == BillingWebhookEvent.DECISION_RECEIVED
    fetch_payment.assert_not_called()


def test_processing_claim_remains_legacy_received_for_safe_rollback():
    choices = dict(BillingWebhookEvent.DECISION_CHOICES)

    assert 'processing' not in choices
    assert BillingWebhookEvent.DECISION_RECEIVED == 'received'


@pytest.mark.django_db
def test_stale_worker_cannot_downgrade_applied_audit_event():
    old_token = uuid.uuid4()
    event = BillingWebhookEvent.objects.create(
        event_type='payment.succeeded',
        object_id='pay_stale_worker',
        idempotency_key='payment.succeeded:pay_stale_worker',
        decision=BillingWebhookEvent.DECISION_APPLIED,
        processed_at=timezone.now(),
    )

    actual_decision = _finalize_webhook_event(
        webhook_event_id=event.pk,
        processing_token=old_token,
        decision=BillingWebhookEvent.DECISION_ERROR,
        reason='late error',
    )

    event.refresh_from_db()
    assert actual_decision == BillingWebhookEvent.DECISION_APPLIED
    assert event.decision == BillingWebhookEvent.DECISION_APPLIED
    assert event.reason == ''


@pytest.mark.django_db
def test_service_rechecks_invoice_payment_id_for_every_operation():
    tenant = make_tenant('invoice-id-recheck')
    first = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('100.00'),
        status=Invoice.STATUS_PENDING,
        yookassa_payment_id='pay_first',
    )
    second = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('100.00'),
        status=Invoice.STATUS_PAID,
        yookassa_payment_id='pay_second',
    )

    assert BillingService.handle_payment_success_webhook(
        first.pk,
        payment_id=second.yookassa_payment_id,
        amount=first.amount,
        currency='RUB',
    ) is False
    assert BillingService.handle_payment_failed_webhook(
        first.pk,
        payment_id=second.yookassa_payment_id,
    ) is False
    assert BillingService.handle_reversal_success(
        invoice_id=second.pk,
        provider_reference='refund_wrong_invoice',
        payment_id=first.yookassa_payment_id,
        amount=Decimal('10.00'),
        currency='RUB',
    ) is None

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.status == Invoice.STATUS_MANUAL_REVIEW
    assert second.status == Invoice.STATUS_PAID
    assert not PaymentReversal.objects.exists()


@pytest.mark.django_db
def test_duplicate_reversal_reference_cannot_switch_invoice():
    first_tenant = make_tenant('reversal-reference-first')
    second_tenant = make_tenant('reversal-reference-second')
    first_invoice = Invoice.objects.create(
        tenant=first_tenant,
        amount=Decimal('100.00'),
        status=Invoice.STATUS_PAID,
        yookassa_payment_id='pay_reversal_reference_first',
    )
    second_invoice = Invoice.objects.create(
        tenant=second_tenant,
        amount=Decimal('100.00'),
        status=Invoice.STATUS_PAID,
        yookassa_payment_id='pay_reversal_reference_second',
    )
    first = BillingService.handle_reversal_success(
        invoice_id=first_invoice.pk,
        provider_reference='refund_global_reference',
        payment_id=first_invoice.yookassa_payment_id,
        amount=Decimal('10.00'),
        currency='RUB',
    )
    collision = BillingService.handle_reversal_success(
        invoice_id=second_invoice.pk,
        provider_reference='refund_global_reference',
        payment_id=second_invoice.yookassa_payment_id,
        amount=Decimal('10.00'),
        currency='RUB',
    )

    second_invoice.refresh_from_db()
    assert first.invoice_id == first_invoice.pk
    assert collision is None
    assert second_invoice.status == Invoice.STATUS_PAID
    assert PaymentReversal.objects.filter(
        provider_reference='refund_global_reference',
    ).count() == 1


@pytest.mark.django_db
def test_reversal_unique_race_rereads_and_fails_closed_on_mismatch():
    first_tenant = make_tenant('reversal-race-first')
    second_tenant = make_tenant('reversal-race-second')
    first_invoice = Invoice.objects.create(
        tenant=first_tenant,
        amount=Decimal('100.00'),
        status=Invoice.STATUS_PAID,
        yookassa_payment_id='pay_reversal_race_first',
    )
    second_invoice = Invoice.objects.create(
        tenant=second_tenant,
        amount=Decimal('100.00'),
        status=Invoice.STATUS_PAID,
        yookassa_payment_id='pay_reversal_race_second',
    )
    authoritative_collision = PaymentReversal(
        invoice=first_invoice,
        provider_reference='refund_race_reference',
        payment_id=first_invoice.yookassa_payment_id,
        amount=Decimal('10.00'),
        currency='RUB',
        kind=PaymentReversal.KIND_REFUND,
        status=PaymentReversal.STATUS_APPLIED,
    )

    with patch(
        'apps.billing.services.PaymentReversal.objects.create',
        side_effect=IntegrityError('simulated unique race'),
    ), patch(
        'apps.billing.services.PaymentReversal.objects.get',
        return_value=authoritative_collision,
    ) as reread:
        collision = BillingService.handle_reversal_success(
            invoice_id=second_invoice.pk,
            provider_reference='refund_race_reference',
            payment_id=second_invoice.yookassa_payment_id,
            amount=Decimal('10.00'),
            currency='RUB',
        )

    second_invoice.refresh_from_db()
    assert collision is None
    assert second_invoice.status == Invoice.STATUS_PAID
    reread.assert_called_once_with(provider_reference='refund_race_reference')
    assert Invoice.objects.filter(pk=second_invoice.pk).exists()


@pytest.mark.django_db
def test_nonempty_payment_id_is_unique_but_blank_legacy_ids_are_allowed():
    tenant = make_tenant('payment-id-unique')
    Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('10.00'),
        yookassa_payment_id='pay_unique',
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.create(
            tenant=tenant,
            amount=Decimal('10.00'),
            yookassa_payment_id='pay_unique',
        )

    Invoice.objects.create(tenant=tenant, amount=Decimal('10.00'))
    Invoice.objects.create(tenant=tenant, amount=Decimal('10.00'))
    assert Invoice.objects.filter(yookassa_payment_id='').count() == 2


@pytest.mark.django_db
def test_notification_is_discarded_when_payment_transaction_rolls_back():
    tenant = make_tenant('notification-rollback')
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('100.00'),
        status=Invoice.STATUS_PENDING,
        yookassa_payment_id='pay_notification_rollback',
    )

    with pytest.raises(RuntimeError), transaction.atomic():
        BillingService.handle_payment_success_webhook(
            invoice.pk,
            payment_id=invoice.yookassa_payment_id,
            amount=invoice.amount,
            currency='RUB',
        )
        raise RuntimeError('force rollback')

    invoice.refresh_from_db()
    assert invoice.status == Invoice.STATUS_PENDING
    assert not BillingOutboxEvent.objects.filter(invoice=invoice).exists()


@pytest.mark.django_db
def test_checkout_endpoints_share_application_rate_limit(
    settings,
    monkeypatch,
):
    rates = settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']
    assert rates['billing_checkout'] == '6/min'
    assert CheckoutView.throttle_classes == [ScopedRateThrottle]
    assert AITopupCheckoutView.throttle_classes == [ScopedRateThrottle]
    assert CheckoutView.throttle_scope == 'billing_checkout'
    assert AITopupCheckoutView.throttle_scope == 'billing_checkout'

    monkeypatch.setattr(
        ScopedRateThrottle,
        'THROTTLE_RATES',
        {**rates, 'billing_checkout': '1/min'},
    )
    tenant = make_tenant('checkout-throttle')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    client = owner_client(tenant)
    cache.clear()
    try:
        with patch.object(
            BillingService,
            'create_payment',
            return_value='https://payments.example/checkout',
        ):
            first = client.post(
                '/api/v1/billing/checkout/',
                {
                    'plan_slug': plan.slug,
                    'period': Subscription.PERIOD_MONTHLY,
                    'idempotency_key': '00000000-0000-4000-8000-000000000004',
                },
                content_type='application/json',
            )
        second = client.post(
            '/api/v1/billing/ai-topup/',
            {
                'package_id': 1,
                'idempotency_key': '00000000-0000-4000-8000-000000000005',
            },
            content_type='application/json',
        )
    finally:
        cache.clear()

    assert first.status_code == 200
    assert second.status_code == 429
