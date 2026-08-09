import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, IntegrityError, transaction
from django.utils import timezone

from apps.billing.ai_wallet import AIWalletService
from apps.billing.models import (
    AICreditPackage, CheckoutIntentKey, Invoice, Plan, Subscription,
)
from apps.billing.services import (
    BillingService,
    CheckoutConflictError,
    CheckoutKeyLimitError,
    CheckoutManualReviewError,
    CheckoutPendingError,
    CheckoutTerminalError,
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
        'purchase_type': Invoice.TYPE_SUBSCRIPTION,
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
    assert tenant.subscription.plan_id == original_plan_id


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
def test_two_paid_subscription_intents_apply_only_captured_version():
    tenant = make_tenant('two-subscription-intents')
    starter = Plan.objects.get(slug=Plan.SLUG_STARTER)
    pro = Plan.objects.get(slug=Plan.SLUG_PRO)
    with patch(
        'apps.billing.yookassa_client.create_payment',
        side_effect=[
            ('pay_first_intent', 'https://pay.example/first-intent'),
            ('pay_second_intent', 'https://pay.example/second-intent'),
        ],
    ):
        BillingService.create_payment(
            tenant,
            starter.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=uuid.uuid4(),
        )
        BillingService.create_payment(
            tenant,
            pro.slug,
            Subscription.PERIOD_MONTHLY,
            'https://app.example/return',
            idempotency_key=uuid.uuid4(),
        )
    first = Invoice.objects.get(yookassa_payment_id='pay_first_intent')
    second = Invoice.objects.get(yookassa_payment_id='pay_second_intent')

    assert BillingService.handle_payment_success_webhook(
        first.pk,
        payment_id=first.yookassa_payment_id,
        amount=first.amount,
        currency=first.currency,
    ) is True
    assert BillingService.handle_payment_success_webhook(
        second.pk,
        payment_id=second.yookassa_payment_id,
        amount=second.amount,
        currency=second.currency,
    ) is False

    second.refresh_from_db()
    tenant.subscription.refresh_from_db()
    assert tenant.subscription.plan_id == starter.pk
    assert second.status == Invoice.STATUS_MANUAL_REVIEW


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
