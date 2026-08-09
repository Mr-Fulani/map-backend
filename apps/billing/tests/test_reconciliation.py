import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.billing.models import BillingWebhookEvent, Invoice, Plan, Subscription
from apps.billing.reconciliation import (
    _finalize_exhausted_event,
    reconcile_yookassa_billing,
)
from apps.billing.services import BillingService, CheckoutPendingError
from apps.billing.webhook_processing import claim_webhook_event_for_reconciliation
from apps.billing.yookassa_client import PaymentSnapshot
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(
        slug,
        slug,
        f'{slug}@test.com',
        'pass12345',
    )
    return tenant


@pytest.mark.django_db
def test_reconciler_resumes_ambiguous_intent_with_same_provider_key(settings):
    settings.YOOKASSA_RECONCILIATION_BASE_DELAY_SECONDS = 1
    tenant = make_tenant('reconcile-intent')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    client_key = uuid.uuid4()
    with patch(
        'apps.billing.yookassa_client.create_payment',
        side_effect=RuntimeError('ambiguous provider result'),
    ) as initial_create, pytest.raises(CheckoutPendingError):
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=client_key,
        )
    provider_key = initial_create.call_args.kwargs['idempotency_key']
    invoice = Invoice.objects.get(checkout_client_key=client_key)
    Invoice.objects.filter(pk=invoice.pk).update(
        next_reconciliation_at=timezone.now() - timedelta(seconds=1),
    )

    with patch(
        'apps.billing.yookassa_client.create_payment',
        return_value=('pay_reconciled_intent', 'https://pay.example/reconciled'),
    ) as retried:
        result = reconcile_yookassa_billing(
            event_ids=[-1],
            invoice_ids=[invoice.pk],
        )

    invoice.refresh_from_db()
    assert result['invoices_resumed'] == 1
    assert retried.call_args.kwargs['idempotency_key'] == provider_key
    assert invoice.yookassa_payment_id == 'pay_reconciled_intent'
    assert invoice.checkout_state == Invoice.CHECKOUT_PROVIDER_CREATED


@pytest.mark.django_db
def test_reconciler_applies_authoritative_succeeded_payment():
    tenant = make_tenant('reconcile-succeeded')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    client_key = uuid.uuid4()
    with patch(
        'apps.billing.yookassa_client.create_payment',
        return_value=('pay_reconcile_success', 'https://pay.example/success'),
    ):
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=client_key,
        )
    invoice = Invoice.objects.get(checkout_client_key=client_key)
    Invoice.objects.filter(pk=invoice.pk).update(
        next_reconciliation_at=timezone.now() - timedelta(seconds=1),
    )
    snapshot = PaymentSnapshot(
        id=invoice.yookassa_payment_id,
        status='succeeded',
        amount=invoice.amount,
        currency=invoice.currency,
        test=False,
    )

    with patch('apps.billing.reconciliation.fetch_payment', return_value=snapshot), \
         patch(
             'apps.billing.webhook_processing.fetch_payment',
             return_value=snapshot,
         ):
        result = reconcile_yookassa_billing(
            event_ids=[-1],
            invoice_ids=[invoice.pk],
        )

    invoice.refresh_from_db()
    assert result['invoices_final'] == 1
    assert invoice.status == Invoice.STATUS_PAID
    event = BillingWebhookEvent.objects.get(
        idempotency_key=f'payment.succeeded:{snapshot.id}',
    )
    assert event.decision == BillingWebhookEvent.DECISION_APPLIED


@pytest.mark.django_db
def test_error_event_is_reprocessed_by_periodic_reconciliation():
    tenant = make_tenant('reconcile-error-event')
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('100.00'),
        currency='RUB',
        status=Invoice.STATUS_PENDING,
        yookassa_payment_id='pay_reconcile_error_event',
        metadata={},
    )
    event = BillingWebhookEvent.objects.create(
        provider='yookassa',
        event_type='payment.succeeded',
        object_id=invoice.yookassa_payment_id,
        payment_id=invoice.yookassa_payment_id,
        idempotency_key=f'payment.succeeded:{invoice.yookassa_payment_id}',
        invoice=invoice,
        tenant=tenant,
        decision=BillingWebhookEvent.DECISION_ERROR,
        next_reconciliation_at=timezone.now() - timedelta(seconds=1),
    )
    snapshot = PaymentSnapshot(
        id=invoice.yookassa_payment_id,
        status='succeeded',
        amount=invoice.amount,
        currency=invoice.currency,
        test=False,
    )

    with patch(
        'apps.billing.webhook_processing.fetch_payment',
        return_value=snapshot,
    ):
        result = reconcile_yookassa_billing(
            event_ids=[event.pk],
            invoice_ids=[-1],
        )

    event.refresh_from_db()
    invoice.refresh_from_db()
    assert result['events_claimed'] == 1
    assert event.decision == BillingWebhookEvent.DECISION_APPLIED
    assert invoice.status == Invoice.STATUS_PAID


@pytest.mark.django_db
def test_exhausted_event_and_pending_invoice_are_closed_atomically(settings):
    settings.YOOKASSA_RECONCILIATION_MAX_ATTEMPTS = 2
    tenant = make_tenant('reconcile-exhausted')
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('100.00'),
        status=Invoice.STATUS_PENDING,
        yookassa_payment_id='pay_reconcile_exhausted',
    )
    event = BillingWebhookEvent.objects.create(
        event_type='payment.succeeded',
        object_id=invoice.yookassa_payment_id,
        payment_id=invoice.yookassa_payment_id,
        idempotency_key=f'payment.succeeded:{invoice.yookassa_payment_id}',
        invoice=invoice,
        tenant=tenant,
        decision=BillingWebhookEvent.DECISION_ERROR,
        reconciliation_attempts=2,
        next_reconciliation_at=timezone.now() - timedelta(seconds=1),
    )

    with patch('apps.billing.webhook_processing.fetch_payment') as fetch_payment:
        result = reconcile_yookassa_billing(
            event_ids=[event.pk],
            invoice_ids=[-1],
        )

    event.refresh_from_db()
    invoice.refresh_from_db()
    fetch_payment.assert_not_called()
    assert result['manual_review'] == 1
    assert event.decision == BillingWebhookEvent.DECISION_MANUAL_REVIEW
    assert invoice.status == Invoice.STATUS_MANUAL_REVIEW


@pytest.mark.django_db
def test_exhausted_refund_event_preserves_completed_partial_refund(settings):
    settings.YOOKASSA_RECONCILIATION_MAX_ATTEMPTS = 2
    tenant = make_tenant('reconcile-partial-refund')
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('100.00'),
        status=Invoice.STATUS_PARTIALLY_REFUNDED,
        yookassa_payment_id='pay_reconcile_partial_refund',
    )
    event = BillingWebhookEvent.objects.create(
        event_type='refund.succeeded',
        object_id='refund_reconcile_partial',
        payment_id=invoice.yookassa_payment_id,
        idempotency_key='refund.succeeded:refund_reconcile_partial',
        invoice=invoice,
        tenant=tenant,
        decision=BillingWebhookEvent.DECISION_ERROR,
        reconciliation_attempts=2,
        next_reconciliation_at=timezone.now() - timedelta(seconds=1),
    )

    result = reconcile_yookassa_billing(
        event_ids=[event.pk],
        invoice_ids=[-1],
    )

    event.refresh_from_db()
    invoice.refresh_from_db()
    assert result['manual_review'] == 0
    assert event.decision == BillingWebhookEvent.DECISION_MANUAL_REVIEW
    assert invoice.status == Invoice.STATUS_PARTIALLY_REFUNDED


@pytest.mark.django_db
def test_fresh_direct_webhook_lease_wins_exhaustion_race(settings):
    settings.YOOKASSA_RECONCILIATION_MAX_ATTEMPTS = 2
    tenant = make_tenant('reconcile-fresh-lease')
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('100.00'),
        status=Invoice.STATUS_PENDING,
        yookassa_payment_id='pay_reconcile_fresh_lease',
    )
    event = BillingWebhookEvent.objects.create(
        event_type='payment.succeeded',
        object_id=invoice.yookassa_payment_id,
        idempotency_key=f'payment.succeeded:{invoice.yookassa_payment_id}',
        invoice=invoice,
        decision=BillingWebhookEvent.DECISION_RECEIVED,
        reconciliation_attempts=2,
        processing_token=uuid.uuid4(),
        processing_started_at=timezone.now(),
    )

    assert _finalize_exhausted_event(event.pk) is None
    event.refresh_from_db()
    invoice.refresh_from_db()
    assert event.decision == BillingWebhookEvent.DECISION_RECEIVED
    assert event.processing_token is not None
    assert invoice.status == Invoice.STATUS_PENDING


@pytest.mark.django_db
def test_force_never_bypasses_hard_reconciliation_attempt_limit(settings):
    settings.YOOKASSA_RECONCILIATION_MAX_ATTEMPTS = 1
    event = BillingWebhookEvent.objects.create(
        event_type='payment.succeeded',
        object_id='pay_force_hard_cap',
        idempotency_key='payment.succeeded:pay_force_hard_cap',
        decision=BillingWebhookEvent.DECISION_ERROR,
        reconciliation_attempts=1,
    )

    _event, token, state = claim_webhook_event_for_reconciliation(
        event.pk,
        force=True,
    )
    assert state == 'exhausted'
    assert token is None


@pytest.mark.django_db
def test_provider_poll_hard_limit_manual_reviews_pending_invoice(settings):
    settings.YOOKASSA_RECONCILIATION_MAX_ATTEMPTS = 1
    tenant = make_tenant('reconcile-invoice-cap')
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('100.00'),
        status=Invoice.STATUS_PENDING,
        yookassa_payment_id='pay_reconcile_invoice_cap',
        checkout_state=Invoice.CHECKOUT_PROVIDER_CREATED,
        reconciliation_attempts=1,
    )

    with patch('apps.billing.reconciliation.fetch_payment') as fetch_payment:
        result = reconcile_yookassa_billing(
            event_ids=[-1],
            invoice_ids=[invoice.pk],
            force=True,
        )

    invoice.refresh_from_db()
    fetch_payment.assert_not_called()
    assert result['manual_review'] == 1
    assert invoice.status == Invoice.STATUS_MANUAL_REVIEW


def test_reconciliation_command_invoice_target_skips_event_sweep():
    with patch(
        'apps.billing.management.commands.reconcile_yookassa.reconcile_yookassa_billing',
        return_value={},
    ) as reconcile:
        call_command('reconcile_yookassa', '--invoice-id', '42')

    reconcile.assert_called_once_with(
        limit=None,
        force=False,
        event_ids=[],
        invoice_ids=[42],
    )


def test_reconciliation_command_event_target_skips_invoice_sweep():
    with patch(
        'apps.billing.management.commands.reconcile_yookassa.reconcile_yookassa_billing',
        return_value={},
    ) as reconcile:
        call_command('reconcile_yookassa', '--event-id', '17')

    reconcile.assert_called_once_with(
        limit=None,
        force=False,
        event_ids=[17],
        invoice_ids=[],
    )
