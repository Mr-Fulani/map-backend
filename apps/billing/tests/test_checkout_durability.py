import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import (
    DEFAULT_DB_ALIAS, IntegrityError, close_old_connections, connection,
    transaction,
)
from django.utils import timezone

from apps.billing.ai_wallet import AIWalletService
from apps.billing.models import (
    AICreditPackage, CheckoutIntentKey, Invoice, Plan, Subscription,
)
from apps.billing.services import (
    ActiveSubscriptionCheckoutError,
    BillingService,
    CheckoutConflictError,
    CheckoutKeyLimitError,
    CheckoutManualReviewError,
    CheckoutPendingError,
    CheckoutTerminalError,
    SubscriptionCheckoutInProgressError,
    add_billing_month,
)
from apps.billing.views import (
    AITopupCheckoutView, CheckoutView, _checkout_error_response,
)
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(
        slug,
        slug,
        f'{slug}@test.com',
        'pass12345',
    )
    return tenant


def test_checkout_views_are_explicitly_non_atomic():
    assert DEFAULT_DB_ALIAS in CheckoutView.dispatch._non_atomic_requests
    assert DEFAULT_DB_ALIAS in AITopupCheckoutView.dispatch._non_atomic_requests


@pytest.mark.django_db
def test_checkout_service_rejects_ambient_application_transaction():
    tenant = make_tenant('checkout-ambient-atomic')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)

    with transaction.atomic(), \
         patch('apps.billing.yookassa_client.create_payment') as provider_create, \
         pytest.raises(RuntimeError, match='внешней DB-транзакции'):
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=uuid.uuid4(),
        )

    provider_create.assert_not_called()


@pytest.mark.django_db
def test_ambiguous_create_is_durable_and_retry_reuses_exact_provider_key():
    tenant = make_tenant('durable-checkout')
    client_key = uuid.uuid4()
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)

    with patch(
        'apps.billing.yookassa_client.create_payment',
        side_effect=RuntimeError('connection lost after send'),
    ) as first_create, pytest.raises(CheckoutPendingError):
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=client_key,
        )

    invoice = Invoice.objects.get(
        tenant=tenant,
        checkout_client_key=client_key,
    )
    first_provider_key = first_create.call_args.kwargs['idempotency_key']
    assert invoice.provider_idempotency_key == first_provider_key
    assert invoice.checkout_state == Invoice.CHECKOUT_PROVIDER_PENDING
    assert invoice.entitlement_snapshot['amount'] == str(plan.price_monthly)
    assert invoice.entitlement_snapshot['expected_subscription_version'] == (
        invoice.expected_subscription_version
    )

    with patch('apps.billing.yookassa_client.create_payment') as premature_retry, \
         pytest.raises(CheckoutPendingError):
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=client_key,
        )
    premature_retry.assert_not_called()

    Invoice.objects.filter(pk=invoice.pk).update(
        next_reconciliation_at=timezone.now() - timedelta(seconds=1),
    )
    with patch(
        'apps.billing.yookassa_client.create_payment',
        return_value=('pay_durable_retry', 'https://pay.example/confirm'),
    ) as retried_create:
        payment_url = BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=client_key,
        )

    invoice.refresh_from_db()
    assert payment_url == 'https://pay.example/confirm'
    assert retried_create.call_args.kwargs['idempotency_key'] == first_provider_key
    assert invoice.yookassa_payment_id == 'pay_durable_retry'
    assert invoice.checkout_confirmation_url == payment_url
    assert Invoice.objects.filter(tenant=tenant, checkout_client_key=client_key).count() == 1


@pytest.mark.django_db
def test_same_client_key_with_different_payload_fails_before_provider_call():
    tenant = make_tenant('checkout-conflict')
    client_key = uuid.uuid4()
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    with patch(
        'apps.billing.yookassa_client.create_payment',
        return_value=('pay_conflict_original', 'https://pay.example/original'),
    ):
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=client_key,
        )

    with patch('apps.billing.yookassa_client.create_payment') as create_payment, \
         pytest.raises(CheckoutConflictError):
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_YEARLY,
            'https://app.example/return',
            idempotency_key=client_key,
        )
    create_payment.assert_not_called()
    assert Invoice.objects.filter(tenant=tenant, checkout_client_key=client_key).count() == 1


@pytest.mark.django_db
def test_same_client_key_is_namespaced_per_tenant():
    first = make_tenant('checkout-key-first')
    second = make_tenant('checkout-key-second')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    client_key = uuid.uuid4()
    with patch(
        'apps.billing.yookassa_client.create_payment',
        side_effect=[
            ('pay_namespaced_first', 'https://pay.example/first'),
            ('pay_namespaced_second', 'https://pay.example/second'),
        ],
    ):
        for tenant in (first, second):
            BillingService.create_payment(
                tenant,
                plan.slug,
                Subscription.PERIOD_MONTHLY,
                'https://app.example/return',
                idempotency_key=client_key,
            )

    invoices = list(Invoice.objects.filter(checkout_client_key=client_key).order_by('pk'))
    assert len(invoices) == 2
    assert invoices[0].provider_idempotency_key != invoices[1].provider_idempotency_key


@pytest.mark.django_db
def test_different_tab_keys_reuse_one_identical_pending_checkout():
    tenant = make_tenant('checkout-multitab-dedup')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    first_key = uuid.uuid4()
    second_key = uuid.uuid4()
    with patch(
        'apps.billing.yookassa_client.create_payment',
        return_value=('pay_multitab_single', 'https://pay.example/multitab'),
    ) as provider_create:
        first_url = BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=first_key,
        )
        second_url = BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=second_key,
        )

    assert first_url == second_url == 'https://pay.example/multitab'
    provider_create.assert_called_once()
    assert Invoice.objects.filter(
        tenant=tenant,
        checkout_payload_hash__gt='',
        status=Invoice.STATUS_PENDING,
    ).count() == 1
    invoice = Invoice.objects.get(tenant=tenant, checkout_payload_hash__gt='')
    assert set(
        CheckoutIntentKey.objects.filter(invoice=invoice).values_list(
            'client_key', flat=True,
        ),
    ) == {first_key, second_key}

    Invoice.objects.filter(pk=invoice.pk).update(status=Invoice.STATUS_PAID)
    with patch('apps.billing.yookassa_client.create_payment') as retry_provider, \
         pytest.raises(CheckoutTerminalError):
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=second_key,
        )
    retry_provider.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('status', 'checkout_state'),
    [
        (Invoice.STATUS_MANUAL_REVIEW, Invoice.CHECKOUT_MANUAL_REVIEW),
        (Invoice.STATUS_PENDING, Invoice.CHECKOUT_LEGACY),
        (Invoice.STATUS_PENDING, Invoice.CHECKOUT_MANUAL_REVIEW),
    ],
)
def test_unresolved_subscription_invoice_blocks_new_provider_payment(
    status,
    checkout_state,
):
    tenant = make_tenant(f'unresolved-{status[:8]}-{checkout_state[:8]}')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    unresolved = Invoice.objects.create(
        tenant=tenant,
        amount=plan.price_monthly,
        currency='RUB',
        status=status,
        purchase_type=Invoice.TYPE_SUBSCRIPTION,
        checkout_state=checkout_state,
        yookassa_payment_id='pay_unresolved_subscription',
        checkout_last_error='Требуется оператор.',
    )

    with patch('apps.billing.yookassa_client.create_payment') as provider_create, \
         pytest.raises(CheckoutManualReviewError) as error:
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=uuid.uuid4(),
        )

    assert error.value.invoice_id == unresolved.pk
    provider_create.assert_not_called()
    assert Invoice.objects.filter(tenant=tenant).count() == 1


@pytest.mark.django_db
def test_unpaid_refund_review_still_blocks_subscription_checkout():
    tenant = make_tenant('unpaid-refund-review-blocks-checkout')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    review = Invoice.objects.create(
        tenant=tenant,
        amount=plan.price_monthly,
        currency='RUB',
        status=Invoice.STATUS_MANUAL_REVIEW,
        purchase_type=Invoice.TYPE_SUBSCRIPTION,
        checkout_state=Invoice.CHECKOUT_MANUAL_REVIEW,
        refund_review_required=True,
        checkout_last_error='Unpaid refund event требует ручной сверки.',
    )

    with patch('apps.billing.yookassa_client.create_payment') as provider_create, \
         pytest.raises(CheckoutManualReviewError) as error:
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=uuid.uuid4(),
        )

    assert error.value.invoice_id == review.pk
    provider_create.assert_not_called()
    assert Invoice.objects.filter(tenant=tenant).count() == 1


@pytest.mark.django_db
def test_manual_review_takes_priority_over_a_retryable_subscription_intent():
    tenant = make_tenant('manual-review-priority')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    Invoice.objects.create(
        tenant=tenant,
        amount=plan.price_monthly,
        currency='RUB',
        status=Invoice.STATUS_PENDING,
        purchase_type=Invoice.TYPE_SUBSCRIPTION,
        checkout_state=Invoice.CHECKOUT_PROVIDER_PENDING,
        checkout_payload_hash='a' * 64,
    )
    review = Invoice.objects.create(
        tenant=tenant,
        amount=plan.price_monthly,
        currency='RUB',
        status=Invoice.STATUS_MANUAL_REVIEW,
        purchase_type=Invoice.TYPE_SUBSCRIPTION,
        checkout_state=Invoice.CHECKOUT_MANUAL_REVIEW,
        checkout_last_error='Сначала разрешите неоднозначное списание.',
    )

    with patch('apps.billing.yookassa_client.create_payment') as provider_create, \
         pytest.raises(CheckoutManualReviewError) as error:
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=uuid.uuid4(),
        )

    assert error.value.invoice_id == review.pk
    provider_create.assert_not_called()


@pytest.mark.django_db
def test_manual_review_takes_priority_for_same_pending_checkout_key():
    tenant = make_tenant('manual-review-same-key-priority')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    client_key = uuid.uuid4()
    with patch(
        'apps.billing.yookassa_client.create_payment',
        side_effect=RuntimeError('ambiguous provider result'),
    ), pytest.raises(CheckoutPendingError):
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=client_key,
        )

    pending = Invoice.objects.get(
        tenant=tenant,
        checkout_client_key=client_key,
    )
    review = Invoice.objects.create(
        tenant=tenant,
        amount=plan.price_monthly,
        currency='RUB',
        status=Invoice.STATUS_MANUAL_REVIEW,
        purchase_type=Invoice.TYPE_SUBSCRIPTION,
        checkout_state=Invoice.CHECKOUT_MANUAL_REVIEW,
        checkout_last_error='Сначала разрешите неоднозначное списание.',
    )

    with patch('apps.billing.yookassa_client.create_payment') as provider_create, \
         pytest.raises(CheckoutManualReviewError) as error:
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=client_key,
        )

    assert error.value.invoice_id == review.pk
    provider_create.assert_not_called()
    pending.refresh_from_db()
    assert pending.status == Invoice.STATUS_PENDING


@pytest.mark.django_db
def test_pending_checkout_cannot_resume_after_paid_subscription_activation():
    tenant = make_tenant('pending-checkout-after-activation')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    client_key = uuid.uuid4()
    with patch(
        'apps.billing.yookassa_client.create_payment',
        side_effect=RuntimeError('ambiguous provider result'),
    ), pytest.raises(CheckoutPendingError):
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=client_key,
        )

    subscription = tenant.subscription
    subscription.status = Subscription.STATUS_ACTIVE
    subscription.current_period_end = timezone.localdate() + timedelta(days=30)
    subscription.billing_version += 1
    subscription.save(update_fields=[
        'status', 'current_period_end', 'billing_version', 'updated_at',
    ])
    invoice = Invoice.objects.get(tenant=tenant, checkout_client_key=client_key)
    Invoice.objects.filter(pk=invoice.pk).update(
        next_reconciliation_at=timezone.now() - timedelta(seconds=1),
    )

    with patch('apps.billing.yookassa_client.create_payment') as provider_create, \
         pytest.raises(CheckoutManualReviewError) as error:
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=client_key,
        )

    provider_create.assert_not_called()
    assert error.value.invoice_id == invoice.pk
    invoice.refresh_from_db()
    assert invoice.status == Invoice.STATUS_MANUAL_REVIEW
    assert 'Подписка стала активной' in invoice.checkout_last_error


@pytest.mark.django_db
def test_post_commit_manual_audit_does_not_overwrite_delayed_paid_state(settings):
    settings.YOOKASSA_RECONCILIATION_MAX_ATTEMPTS = 1
    tenant = make_tenant('manual-audit-does-not-overwrite-paid')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    client_key = uuid.uuid4()
    with patch(
        'apps.billing.yookassa_client.create_payment',
        side_effect=RuntimeError('ambiguous provider result'),
    ), pytest.raises(CheckoutPendingError):
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=client_key,
        )
    invoice = Invoice.objects.get(tenant=tenant, checkout_client_key=client_key)
    Invoice.objects.filter(pk=invoice.pk).update(
        next_reconciliation_at=timezone.now() - timedelta(seconds=1),
    )
    audit_only = BillingService._audit_checkout_manual_review

    def complete_payment_before_delayed_audit(invoice_id, tenant_id, reason):
        # Simulate an authoritative transaction committing after the first
        # manual-review write but before its best-effort post-commit audit.
        Invoice.objects.filter(pk=invoice_id).update(
            status=Invoice.STATUS_PAID,
            checkout_state=Invoice.CHECKOUT_PROVIDER_CREATED,
            paid_at=timezone.now(),
            checkout_last_error='',
            next_reconciliation_at=None,
        )
        audit_only(invoice_id, tenant_id, reason)

    with patch.object(
        BillingService,
        '_audit_checkout_manual_review',
        side_effect=complete_payment_before_delayed_audit,
    ):
        with patch(
            'apps.billing.yookassa_client.create_payment',
        ) as provider_create, pytest.raises(CheckoutManualReviewError):
            BillingService.create_payment(
                tenant,
                plan.slug,
                Subscription.PERIOD_MONTHLY,
                'https://app.example/return',
                idempotency_key=client_key,
            )

    provider_create.assert_not_called()
    invoice.refresh_from_db()
    assert invoice.status == Invoice.STATUS_PAID
    assert invoice.checkout_state == Invoice.CHECKOUT_PROVIDER_CREATED
    assert invoice.paid_at is not None
    assert invoice.checkout_last_error == ''


@pytest.mark.django_db
def test_paid_refund_review_does_not_block_expired_subscription_checkout():
    tenant = make_tenant('paid-refund-review-not-checkout-blocker')
    subscription = tenant.subscription
    subscription.status = Subscription.STATUS_PAST_DUE
    subscription.current_period_end = timezone.localdate() - timedelta(days=1)
    subscription.save(update_fields=[
        'status', 'current_period_end', 'updated_at',
    ])
    Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('14900.00'),
        currency='RUB',
        status=Invoice.STATUS_MANUAL_REVIEW,
        purchase_type=Invoice.TYPE_SUBSCRIPTION,
        checkout_state=Invoice.CHECKOUT_MANUAL_REVIEW,
        yookassa_payment_id='pay_historical_refund_review',
        paid_at=timezone.now() - timedelta(days=40),
        refund_review_required=True,
    )
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)

    with patch(
        'apps.billing.yookassa_client.create_payment',
        return_value=(
            'pay_after_refund_review',
            'https://pay.example/after-refund-review',
        ),
    ) as provider_create:
        payment_url = BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=uuid.uuid4(),
        )

    assert payment_url == 'https://pay.example/after-refund-review'
    provider_create.assert_called_once()
    assert Invoice.objects.filter(tenant=tenant).count() == 2


@pytest.mark.django_db
def test_authoritative_success_recovers_unsettled_manual_checkout():
    tenant = make_tenant('manual-checkout-success-recovery')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    with patch(
        'apps.billing.yookassa_client.create_payment',
        return_value=(
            'pay_manual_success_recovery',
            'https://pay.example/manual-success-recovery',
        ),
    ):
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=uuid.uuid4(),
        )
    invoice = Invoice.objects.get(
        tenant=tenant,
        yookassa_payment_id='pay_manual_success_recovery',
    )
    BillingService.mark_invoice_manual_review(
        invoice.pk,
        'Ожидается авторитетная сверка provider state.',
    )

    assert BillingService.handle_payment_success_webhook(
        invoice.pk,
        payment_id=invoice.yookassa_payment_id,
        amount=invoice.amount,
        currency=invoice.currency,
    ) is True

    invoice.refresh_from_db()
    assert invoice.status == Invoice.STATUS_PAID
    assert invoice.checkout_state == Invoice.CHECKOUT_PROVIDER_CREATED
    assert invoice.paid_at is not None
    assert invoice.checkout_last_error == ''


@pytest.mark.django_db
def test_authoritative_cancel_recovers_unsettled_manual_checkout():
    tenant = make_tenant('manual-checkout-cancel-recovery')
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('1490.00'),
        currency='RUB',
        status=Invoice.STATUS_MANUAL_REVIEW,
        purchase_type=Invoice.TYPE_SUBSCRIPTION,
        checkout_state=Invoice.CHECKOUT_MANUAL_REVIEW,
        yookassa_payment_id='pay_manual_cancel_recovery',
        checkout_last_error='Ожидается авторитетная отмена.',
    )

    assert BillingService.handle_payment_failed_webhook(
        invoice.pk,
        payment_id=invoice.yookassa_payment_id,
    ) is True

    invoice.refresh_from_db()
    assert invoice.status == Invoice.STATUS_FAILED
    assert invoice.checkout_state == Invoice.CHECKOUT_PROVIDER_CREATED
    assert invoice.checkout_last_error == ''


@pytest.mark.django_db(transaction=True)
def test_pending_to_manual_transition_cannot_open_new_checkout_window():
    """One SQL snapshot must retain either side of pending -> manual_review."""
    if connection.vendor != 'postgresql':
        pytest.skip('The regression verifies PostgreSQL MVCC concurrency.')

    tenant = make_tenant('manual-review-transition-race')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    return_url = 'https://app.example/return'
    first_key = uuid.uuid4()
    with patch(
        'apps.billing.yookassa_client.create_payment',
        side_effect=RuntimeError('ambiguous provider result'),
    ), pytest.raises(CheckoutPendingError):
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            return_url,
            idempotency_key=first_key,
        )
    pending = Invoice.objects.get(tenant=tenant, checkout_client_key=first_key)

    unresolved_snapshot_executed = threading.Event()
    manual_transition_committed = threading.Event()

    class PauseAfterUnresolvedSnapshot:
        paused = False

        def __call__(self, execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            values = tuple(params or ())
            if (
                not self.paused
                and 'billing_invoice' in sql
                and Invoice.STATUS_MANUAL_REVIEW in values
                and Invoice.CHECKOUT_LEGACY in values
            ):
                self.paused = True
                unresolved_snapshot_executed.set()
                if not manual_transition_committed.wait(timeout=15):
                    raise AssertionError('manual-review transition timed out')
            return result

    def retry_with_new_browser_key():
        close_old_connections()
        try:
            local_tenant = type(tenant).objects.get(pk=tenant.pk)
            with connection.execute_wrapper(PauseAfterUnresolvedSnapshot()):
                try:
                    BillingService.create_payment(
                        local_tenant,
                        plan.slug,
                        Subscription.PERIOD_MONTHLY,
                        return_url,
                        idempotency_key=uuid.uuid4(),
                    )
                except CheckoutManualReviewError as exc:
                    return exc.invoice_id
            return None
        finally:
            close_old_connections()

    with patch('apps.billing.yookassa_client.create_payment') as provider_create, \
         ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(retry_with_new_browser_key)
        assert unresolved_snapshot_executed.wait(timeout=15)
        BillingService.mark_invoice_manual_review(
            pending.pk,
            'Неоднозначный provider result требует оператора.',
        )
        manual_transition_committed.set()
        assert future.result(timeout=15) == pending.pk

    provider_create.assert_not_called()
    pending.refresh_from_db()
    assert pending.status == Invoice.STATUS_MANUAL_REVIEW
    assert Invoice.objects.filter(tenant=tenant).count() == 1


@pytest.mark.django_db
def test_terminal_checkout_key_never_returns_stale_confirmation_url():
    tenant = make_tenant('checkout-terminal-key')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    client_key = uuid.uuid4()
    with patch(
        'apps.billing.yookassa_client.create_payment',
        return_value=('pay_terminal_key', 'https://pay.example/terminal'),
    ):
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=client_key,
        )
    invoice = Invoice.objects.get(checkout_client_key=client_key)
    Invoice.objects.filter(pk=invoice.pk).update(status=Invoice.STATUS_PAID)

    with patch('apps.billing.yookassa_client.create_payment') as provider_create, \
         pytest.raises(CheckoutTerminalError) as raised:
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=client_key,
        )

    provider_create.assert_not_called()
    assert raised.value.invoice_id == invoice.pk
    assert raised.value.invoice_status == Invoice.STATUS_PAID


def test_terminal_checkout_error_is_the_only_key_rotation_signal():
    response = _checkout_error_response(
        CheckoutTerminalError(42, Invoice.STATUS_FAILED),
    )

    assert response.status_code == 409
    assert response.data == {
        'status': 'error',
        'code': 'checkout_terminal',
        'message': 'Checkout intent уже завершён со статусом failed.',
        'data': {
            'invoice_id': 42,
            'invoice_status': Invoice.STATUS_FAILED,
            'retryable': False,
            'rotate_idempotency_key': True,
        },
    }


def test_checkout_key_limit_error_requires_reusing_an_existing_key():
    response = _checkout_error_response(CheckoutKeyLimitError(42))

    assert response.status_code == 409
    assert response.data == {
        'status': 'error',
        'code': 'checkout_key_limit',
        'message': (
            'Для checkout intent исчерпан лимит ключей; '
            'повторите запрос с ранее выданным idempotency_key.'
        ),
        'data': {
            'invoice_id': 42,
            'retryable': False,
            'reuse_idempotency_key': True,
        },
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('target_slug', 'target_period'),
    [
        (Plan.SLUG_BUSINESS, Subscription.PERIOD_MONTHLY),
        (Plan.SLUG_STARTER, Subscription.PERIOD_YEARLY),
    ],
)
def test_active_paid_subscription_checkout_fails_before_invoice_and_provider(
    target_slug,
    target_period,
):
    tenant = make_tenant('checkout-active-paid-blocked')
    subscription = tenant.subscription
    today = timezone.localdate()
    subscription.status = Subscription.STATUS_ACTIVE
    subscription.current_period_start = today - timedelta(days=5)
    subscription.current_period_end = today + timedelta(days=20)
    subscription.save(update_fields=[
        'status', 'current_period_start', 'current_period_end', 'updated_at',
    ])
    target = Plan.objects.get(slug=target_slug, is_active=True)

    with patch('apps.billing.yookassa_client.create_payment') as provider_create, \
         pytest.raises(ActiveSubscriptionCheckoutError) as raised:
        BillingService.create_payment(
            tenant,
            target.slug,
            target_period,
            'https://app.example/return',
            idempotency_key=uuid.uuid4(),
        )

    provider_create.assert_not_called()
    assert not Invoice.objects.filter(tenant=tenant).exists()
    assert raised.value.plan_slug == subscription.plan.slug
    assert raised.value.billing_period == subscription.billing_period
    assert raised.value.current_period_end == subscription.current_period_end


def test_active_subscription_checkout_error_response_contract():
    period_end = timezone.localdate() + timedelta(days=20)
    response = _checkout_error_response(ActiveSubscriptionCheckoutError(
        plan_slug=Plan.SLUG_BUSINESS,
        billing_period=Subscription.PERIOD_MONTHLY,
        current_period_end=period_end,
    ))

    assert response.status_code == 409
    assert response.data == {
        'status': 'error',
        'code': 'active_subscription_change_not_supported',
        'message': (
            'Изменение или продление действующей платной подписки '
            'до окончания текущего периода недоступно.'
        ),
        'data': {
            'retryable': False,
            'current_plan_slug': Plan.SLUG_BUSINESS,
            'current_billing_period': Subscription.PERIOD_MONTHLY,
            'current_period_end': period_end.isoformat(),
        },
    }


def test_subscription_checkout_in_progress_error_response_contract():
    response = _checkout_error_response(SubscriptionCheckoutInProgressError(73))

    assert response.status_code == 409
    assert response.data == {
        'status': 'error',
        'code': 'subscription_checkout_in_progress',
        'message': (
            'Уже есть незавершённый checkout подписки; '
            'завершите или дождитесь его финального статуса.'
        ),
        'data': {
            'invoice_id': 73,
            'retryable': False,
            'reuse_existing_checkout': True,
        },
    }


@pytest.mark.django_db(transaction=True)
def test_concurrent_different_subscription_checkouts_create_one_payment():
    """PostgreSQL row locking admits only one unfinished purchase per tenant."""
    tenant = make_tenant('checkout-concurrent-subscription-guard')
    plan_slugs = [Plan.SLUG_STARTER, Plan.SLUG_PRO]
    start_together = threading.Barrier(2)

    def checkout(plan_slug):
        close_old_connections()
        try:
            local_tenant = type(tenant).objects.get(pk=tenant.pk)
            start_together.wait(timeout=15)
            try:
                return (
                    'created',
                    BillingService.create_payment(
                        local_tenant,
                        plan_slug,
                        Subscription.PERIOD_MONTHLY,
                        'https://app.example/return',
                        idempotency_key=uuid.uuid4(),
                    ),
                )
            except SubscriptionCheckoutInProgressError as exc:
                return 'blocked', exc.invoice_id
        finally:
            close_old_connections()

    with patch(
        'apps.billing.yookassa_client.create_payment',
        return_value=('pay_concurrent_guard', 'https://pay.example/concurrent'),
    ) as provider_create, ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(checkout, plan_slugs))

    assert {result[0] for result in results} == {'created', 'blocked'}
    provider_create.assert_called_once()
    invoice = Invoice.objects.get(tenant=tenant)
    assert invoice.yookassa_payment_id == 'pay_concurrent_guard'
    assert next(value for state, value in results if state == 'blocked') == invoice.pk


@pytest.mark.django_db
def test_active_checkout_has_bounded_client_key_aliases(settings):
    settings.BILLING_CHECKOUT_MAX_KEYS_PER_INVOICE = 2
    tenant = make_tenant('checkout-key-alias-cap')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    first_key = uuid.uuid4()
    second_key = uuid.uuid4()
    with patch(
        'apps.billing.yookassa_client.create_payment',
        return_value=('pay_alias_cap', 'https://pay.example/alias-cap'),
    ) as provider_create:
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=first_key,
        )
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=second_key,
        )
        with pytest.raises(CheckoutKeyLimitError):
            BillingService.create_payment(
                tenant,
                plan.slug,
                Subscription.PERIOD_MONTHLY,
                'https://app.example/return',
                idempotency_key=uuid.uuid4(),
            )

        # A key that was already accepted remains idempotent at the cap.
        assert BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=second_key,
        ) == 'https://pay.example/alias-cap'

    provider_create.assert_called_once()
    invoice = Invoice.objects.get(yookassa_payment_id='pay_alias_cap')
    assert CheckoutIntentKey.objects.filter(invoice=invoice).count() == 2


@pytest.mark.django_db
def test_checkout_alias_cap_reserves_unregistered_canonical_key(settings):
    """A crash before canonical binding must not let an alias steal its slot."""
    settings.BILLING_CHECKOUT_MAX_KEYS_PER_INVOICE = 2
    tenant = make_tenant('checkout-key-canonical-reservation')
    canonical_key = uuid.uuid4()
    existing_alias = uuid.uuid4()
    payload_hash = 'b' * 64
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('100.00'),
        status=Invoice.STATUS_PENDING,
        purchase_type=Invoice.TYPE_SUBSCRIPTION,
        checkout_client_key=canonical_key,
        provider_idempotency_key=str(uuid.uuid4()),
        checkout_payload_hash=payload_hash,
        checkout_state=Invoice.CHECKOUT_INTENT_CREATED,
    )
    CheckoutIntentKey.objects.create(
        tenant=tenant,
        invoice=invoice,
        client_key=existing_alias,
        checkout_payload_hash=payload_hash,
    )

    with pytest.raises(CheckoutKeyLimitError):
        BillingService._bind_checkout_client_key(
            tenant_id=tenant.pk,
            invoice=invoice,
            client_key=uuid.uuid4(),
            payload_hash=payload_hash,
        )

    BillingService._bind_checkout_client_key(
        tenant_id=tenant.pk,
        invoice=invoice,
        client_key=canonical_key,
        payload_hash=payload_hash,
    )
    assert set(
        CheckoutIntentKey.objects.filter(invoice=invoice).values_list(
            'client_key', flat=True,
        ),
    ) == {canonical_key, existing_alias}


@pytest.mark.django_db
def test_database_rejects_duplicate_active_checkout_payload():
    tenant = make_tenant('checkout-payload-constraint')
    common = {
        'tenant': tenant,
        'amount': Decimal('100.00'),
        'status': Invoice.STATUS_PENDING,
        'purchase_type': Invoice.TYPE_AI_TOPUP,
        'checkout_payload_hash': 'a' * 64,
        'checkout_state': Invoice.CHECKOUT_INTENT_CREATED,
    }
    Invoice.objects.create(
        **common,
        checkout_client_key=uuid.uuid4(),
        provider_idempotency_key=str(uuid.uuid4()),
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.create(
            **common,
            checkout_client_key=uuid.uuid4(),
            provider_idempotency_key=str(uuid.uuid4()),
        )


@pytest.mark.django_db
def test_database_rejects_two_active_subscription_checkouts_per_tenant():
    tenant = make_tenant('checkout-subscription-constraint')
    common = {
        'tenant': tenant,
        'amount': Decimal('100.00'),
        'status': Invoice.STATUS_PENDING,
        'purchase_type': Invoice.TYPE_SUBSCRIPTION,
        'checkout_state': Invoice.CHECKOUT_INTENT_CREATED,
    }
    Invoice.objects.create(
        **common,
        checkout_client_key=uuid.uuid4(),
        provider_idempotency_key=str(uuid.uuid4()),
        checkout_payload_hash='c' * 64,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.create(
            **common,
            checkout_client_key=uuid.uuid4(),
            provider_idempotency_key=str(uuid.uuid4()),
            checkout_payload_hash='d' * 64,
        )


@pytest.mark.django_db
def test_invoice_snapshot_is_immutable_and_payment_id_is_write_once():
    tenant = make_tenant('immutable-invoice')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=plan.price_monthly,
        entitlement_snapshot={
            'schema': 1,
            'purchase_type': Invoice.TYPE_SUBSCRIPTION,
            'amount': str(plan.price_monthly),
            'currency': 'RUB',
        },
    )
    invoice.yookassa_payment_id = 'pay_write_once'
    invoice.save(update_fields=['yookassa_payment_id'])

    invoice.amount += Decimal('1.00')
    with pytest.raises(ValidationError):
        invoice.save(update_fields=['amount'])
    invoice.refresh_from_db()
    invoice.yookassa_payment_id = 'pay_reassigned'
    with pytest.raises(ValidationError):
        invoice.save(update_fields=['yookassa_payment_id'])


@pytest.mark.django_db
def test_ai_fulfillment_uses_snapshot_after_package_changes():
    tenant = make_tenant('ai-snapshot')
    package = AICreditPackage.objects.filter(is_active=True).first()
    original_credits = package.credits
    client_key = uuid.uuid4()
    with patch(
        'apps.billing.yookassa_client.create_payment',
        return_value=('pay_ai_snapshot', 'https://pay.example/ai'),
    ):
        BillingService.create_ai_topup_payment(
            tenant,
            package.pk,
            'https://app.example/return',
            idempotency_key=client_key,
        )
    AICreditPackage.objects.filter(pk=package.pk).update(
        credits=original_credits * 10,
        price_rub=package.price_rub * 10,
        name='Changed after checkout',
        is_active=False,
    )
    invoice = Invoice.objects.get(checkout_client_key=client_key)

    assert BillingService.handle_payment_success_webhook(
        invoice.pk,
        payment_id=invoice.yookassa_payment_id,
        amount=invoice.amount,
        currency=invoice.currency,
    ) is True
    assert AIWalletService.summary(tenant)['purchased'] == original_credits


@pytest.mark.django_db
def test_malformed_durable_ai_snapshot_fails_closed_without_package_fallback():
    tenant = make_tenant('ai-malformed-snapshot')
    package = AICreditPackage.objects.filter(is_active=True).first()
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=package.price_rub,
        currency='RUB',
        purchase_type=Invoice.TYPE_AI_TOPUP,
        metadata={'package_id': str(package.pk)},
        yookassa_payment_id='pay_ai_malformed_snapshot',
        entitlement_snapshot={
            'schema': 1,
            'purchase_type': Invoice.TYPE_AI_TOPUP,
            'amount': str(package.price_rub),
            'currency': 'RUB',
            'package': {'credits': 'not-a-number'},
        },
    )

    assert BillingService.handle_payment_success_webhook(
        invoice.pk,
        payment_id=invoice.yookassa_payment_id,
        amount=invoice.amount,
        currency=invoice.currency,
    ) is False
    invoice.refresh_from_db()
    assert invoice.status == Invoice.STATUS_MANUAL_REVIEW
    assert AIWalletService.summary(tenant)['purchased'] == 0


@pytest.mark.django_db
def test_stale_subscription_intent_is_manual_reviewed():
    tenant = make_tenant('stale-subscription-intent')
    original_plan_id = tenant.subscription.plan_id
    target = Plan.objects.get(slug=Plan.SLUG_STARTER)
    client_key = uuid.uuid4()
    with patch(
        'apps.billing.yookassa_client.create_payment',
        return_value=('pay_stale_subscription', 'https://pay.example/stale'),
    ):
        BillingService.create_payment(
            tenant,
            target.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=client_key,
        )
    invoice = Invoice.objects.get(checkout_client_key=client_key)
    Subscription.objects.filter(tenant=tenant).update(
        billing_version=invoice.expected_subscription_version + 1,
    )

    assert BillingService.handle_payment_success_webhook(
        invoice.pk,
        payment_id=invoice.yookassa_payment_id,
        amount=invoice.amount,
        currency=invoice.currency,
    ) is False
    invoice.refresh_from_db()
    tenant.subscription.refresh_from_db()
    assert invoice.status == Invoice.STATUS_MANUAL_REVIEW
    assert 'expected_version=' in invoice.checkout_last_error
    assert 'actual_version=' in invoice.checkout_last_error
    assert tenant.subscription.plan_id == original_plan_id


@pytest.mark.django_db
def test_billing_only_recovery_appends_term_without_losing_paid_days():
    tenant = make_tenant('billing-only-term-preserved')
    subscription = tenant.subscription
    today = timezone.localdate()
    original_start = today - timedelta(days=10)
    original_end = today + timedelta(days=8)
    subscription.status = Subscription.STATUS_PAST_DUE
    subscription.current_period_start = original_start
    subscription.current_period_end = original_end
    subscription.save(update_fields=[
        'status', 'current_period_start', 'current_period_end', 'updated_at',
    ])
    assert subscription.access_mode == Subscription.ACCESS_BILLING_ONLY
    target = Plan.objects.get(slug=Plan.SLUG_STARTER)

    with patch(
        'apps.billing.yookassa_client.create_payment',
        return_value=('pay_preserve_term', 'https://pay.example/preserve-term'),
    ) as provider_create:
        BillingService.create_payment(
            tenant,
            target.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=uuid.uuid4(),
        )

    invoice = Invoice.objects.get(yookassa_payment_id='pay_preserve_term')
    assert BillingService.handle_payment_success_webhook(
        invoice.pk,
        payment_id=invoice.yookassa_payment_id,
        amount=invoice.amount,
        currency=invoice.currency,
    ) is True

    provider_create.assert_called_once()
    subscription.refresh_from_db()
    assert subscription.status == Subscription.STATUS_ACTIVE
    assert subscription.plan_id == target.pk
    assert subscription.current_period_start == today
    assert subscription.current_period_end == add_billing_month(original_end)
    assert subscription.current_period_end > original_end
    assert subscription.ai_period_start == today
    assert subscription.ai_period_end == add_billing_month(today)
    assert BillingService.refresh_ai_credit_period(
        subscription.pk,
        today,
    ) is False


@pytest.mark.django_db
def test_subscription_fulfillment_preserves_individual_ai_limit():
    tenant = make_tenant('subscription-limit-override')
    tenant.ai_credit_limit_override = 12345
    tenant.save(update_fields=['ai_credit_limit_override'])
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    client_key = uuid.uuid4()
    with patch(
        'apps.billing.yookassa_client.create_payment',
        return_value=('pay_limit_override', 'https://pay.example/limit'),
    ):
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=client_key,
        )
    invoice = Invoice.objects.get(checkout_client_key=client_key)
    original_amount = invoice.amount
    Plan.objects.filter(pk=plan.pk).update(
        price_monthly=plan.price_monthly * 10,
        is_active=False,
    )

    assert BillingService.handle_payment_success_webhook(
        invoice.pk,
        payment_id=invoice.yookassa_payment_id,
        amount=invoice.amount,
        currency=invoice.currency,
    ) is True
    invoice.refresh_from_db()
    assert invoice.amount == original_amount
    assert AIWalletService.summary(tenant)['included_limit'] == Decimal('12345')


@pytest.mark.django_db
def test_different_pending_subscription_intent_is_blocked_before_provider():
    tenant = make_tenant('different-subscription-intent-blocked')
    starter = Plan.objects.get(slug=Plan.SLUG_STARTER)
    pro = Plan.objects.get(slug=Plan.SLUG_PRO)
    with patch(
        'apps.billing.yookassa_client.create_payment',
        return_value=('pay_first_intent', 'https://pay.example/first-intent'),
    ) as provider_create:
        BillingService.create_payment(
            tenant,
            starter.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=uuid.uuid4(),
        )
        with pytest.raises(SubscriptionCheckoutInProgressError) as raised:
            BillingService.create_payment(
                tenant,
                pro.slug,
                Subscription.PERIOD_MONTHLY,
                'https://app.example/return',
                idempotency_key=uuid.uuid4(),
            )

    first = Invoice.objects.get(yookassa_payment_id='pay_first_intent')
    provider_create.assert_called_once()
    assert Invoice.objects.filter(tenant=tenant).count() == 1
    assert raised.value.invoice_id == first.pk

    assert BillingService.handle_payment_success_webhook(
        first.pk,
        payment_id=first.yookassa_payment_id,
        amount=first.amount,
        currency=first.currency,
    ) is True

    tenant.subscription.refresh_from_db()
    assert tenant.subscription.plan_id == starter.pk


@pytest.mark.django_db
def test_intent_outside_provider_idempotency_horizon_fails_closed():
    tenant = make_tenant('intent-idempotency-horizon')
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    client_key = uuid.uuid4()
    with patch(
        'apps.billing.yookassa_client.create_payment',
        side_effect=RuntimeError('ambiguous'),
    ), pytest.raises(CheckoutPendingError):
        BillingService.create_payment(
            tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=client_key,
        )
    invoice = Invoice.objects.get(checkout_client_key=client_key)
    Invoice.objects.filter(pk=invoice.pk).update(
        checkout_first_attempt_at=timezone.now() - timedelta(hours=24),
        next_reconciliation_at=timezone.now() - timedelta(seconds=1),
    )

    with patch('apps.billing.yookassa_client.create_payment') as create_payment, \
         pytest.raises(CheckoutManualReviewError):
        BillingService._resume_checkout_intent(
            invoice.pk,
            respect_backoff=True,
        )
    create_payment.assert_not_called()
    invoice.refresh_from_db()
    assert invoice.status == Invoice.STATUS_MANUAL_REVIEW


@pytest.mark.django_db
def test_legacy_subscription_without_metadata_preserves_yearly_term():
    tenant = make_tenant('legacy-yearly-payment')
    sub = tenant.subscription
    original_start = timezone.localdate() - timedelta(days=30)
    original_end = timezone.localdate() + timedelta(days=335)
    sub.billing_period = Subscription.PERIOD_YEARLY
    sub.current_period_start = original_start
    sub.current_period_end = original_end
    sub.ai_period_start = original_start
    sub.ai_period_end = original_start + timedelta(days=31)
    sub.status = Subscription.STATUS_PAST_DUE
    sub.save()
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('100.00'),
        currency='RUB',
        status=Invoice.STATUS_PENDING,
        yookassa_payment_id='pay_legacy_yearly',
        metadata={},
    )

    assert BillingService.handle_payment_success_webhook(
        invoice.pk,
        payment_id=invoice.yookassa_payment_id,
        amount=invoice.amount,
        currency=invoice.currency,
    ) is True
    sub.refresh_from_db()
    invoice.refresh_from_db()
    assert sub.status == Subscription.STATUS_ACTIVE
    assert sub.billing_period == Subscription.PERIOD_YEARLY
    assert sub.current_period_start == original_start
    assert sub.current_period_end == original_end
    assert invoice.entitlement_snapshot['legacy_status_only'] is True


@pytest.mark.django_db
def test_billing_audit_failure_does_not_poison_payment_transaction():
    tenant = make_tenant('billing-log-savepoint')
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('100.00'),
        currency='RUB',
        status=Invoice.STATUS_PENDING,
        yookassa_payment_id='pay_log_savepoint',
        metadata={},
    )
    with patch(
        'apps.sync.models.SyncLog.objects.create',
        side_effect=IntegrityError('audit unavailable'),
    ):
        assert BillingService.handle_payment_success_webhook(
            invoice.pk,
            payment_id=invoice.yookassa_payment_id,
            amount=invoice.amount,
            currency=invoice.currency,
        ) is True

    invoice.refresh_from_db()
    assert invoice.status == Invoice.STATUS_PAID


@pytest.mark.django_db
def test_provider_payment_collision_manual_reviews_both_invoices():
    first_tenant = make_tenant('provider-collision-first')
    second_tenant = make_tenant('provider-collision-second')
    owner = Invoice.objects.create(
        tenant=first_tenant,
        amount=Decimal('100.00'),
        status=Invoice.STATUS_PENDING,
        yookassa_payment_id='pay_provider_collision',
    )
    plan = Plan.objects.get(slug=Plan.SLUG_STARTER)
    with patch(
        'apps.billing.yookassa_client.create_payment',
        return_value=('pay_provider_collision', 'https://pay.example/collision'),
    ), pytest.raises(CheckoutManualReviewError):
        BillingService.create_payment(
            second_tenant,
            plan.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=uuid.uuid4(),
        )

    owner.refresh_from_db()
    contender = Invoice.objects.get(tenant=second_tenant)
    assert owner.status == Invoice.STATUS_MANUAL_REVIEW
    assert contender.status == Invoice.STATUS_MANUAL_REVIEW
