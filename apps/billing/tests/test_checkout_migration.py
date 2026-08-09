"""Migration coverage for durable and mutually exclusive checkout intents."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


@pytest.mark.django_db(transaction=True)
def test_durable_checkout_migration_freezes_or_quarantines_pending_topups():
    migrate_from = [('billing', '0012_harden_yookassa_webhooks')]
    migrate_to = [('billing', '0013_durable_checkout_intents')]
    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    old_apps = executor.loader.project_state(migrate_from).apps
    Tenant = old_apps.get_model('tenants', 'Tenant')
    Package = old_apps.get_model('billing', 'AICreditPackage')
    Invoice = old_apps.get_model('billing', 'Invoice')
    tenant = Tenant.objects.create(name='Legacy Topups', slug='legacy-topups')
    package = Package.objects.create(
        name='Legacy package',
        credits=Decimal('250.0000'),
        price_rub=Decimal('500.00'),
        is_active=False,
    )
    frozen_id = Invoice.objects.create(
        tenant=tenant,
        amount=package.price_rub,
        currency='RUB',
        status='pending',
        purchase_type='ai_topup',
        yookassa_payment_id='pay_legacy_topup_safe',
        metadata={'package_id': str(package.pk)},
    ).pk
    quarantined_id = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('499.00'),
        currency='RUB',
        status='pending',
        purchase_type='ai_topup',
        yookassa_payment_id='pay_legacy_topup_ambiguous',
        metadata={'package_id': str(package.pk)},
    ).pk
    changed_package = Package.objects.create(
        name='Changed legacy package',
        credits=Decimal('100.0000'),
        price_rub=Decimal('500.00'),
        is_active=False,
    )
    changed_package_invoice_id = Invoice.objects.create(
        tenant=tenant,
        amount=changed_package.price_rub,
        currency='RUB',
        status='pending',
        purchase_type='ai_topup',
        yookassa_payment_id='pay_legacy_topup_changed_package',
        metadata={'package_id': str(changed_package.pk)},
    ).pk
    Package.objects.filter(pk=changed_package.pk).update(
        credits=Decimal('200.0000'),
        updated_at=timezone.now() + timedelta(seconds=1),
    )
    unpaid_id = Invoice.objects.create(
        tenant=tenant,
        amount=package.price_rub,
        currency='RUB',
        status='pending',
        purchase_type='ai_topup',
        yookassa_payment_id='',
        metadata={'package_id': str(package.pk)},
    ).pk

    try:
        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        new_apps = executor.loader.project_state(migrate_to).apps
        MigratedInvoice = new_apps.get_model('billing', 'Invoice')

        frozen = MigratedInvoice.objects.get(pk=frozen_id)
        quarantined = MigratedInvoice.objects.get(pk=quarantined_id)
        changed_package_invoice = MigratedInvoice.objects.get(
            pk=changed_package_invoice_id,
        )
        unpaid = MigratedInvoice.objects.get(pk=unpaid_id)
        assert frozen.status == 'pending'
        assert Decimal(frozen.entitlement_snapshot['package']['credits']) == Decimal(
            '250.0000',
        )
        assert frozen.entitlement_snapshot['legacy_pending_backfill'] is True
        assert quarantined.status == 'manual_review'
        assert quarantined.checkout_state == 'manual_review'
        assert 'ручная проверка' in quarantined.checkout_last_error
        assert changed_package_invoice.status == 'manual_review'
        assert changed_package_invoice.checkout_state == 'manual_review'
        assert changed_package_invoice.entitlement_snapshot == {}
        assert unpaid.status == 'pending'
        assert unpaid.entitlement_snapshot == {}
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_single_subscription_checkout_migration_quarantines_every_duplicate():
    migrate_from = [('billing', '0016_billing_outbox_dead_letter')]
    migrate_to = [('billing', '0017_single_active_subscription_checkout')]
    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    old_apps = executor.loader.project_state(migrate_from).apps
    Tenant = old_apps.get_model('tenants', 'Tenant')
    Invoice = old_apps.get_model('billing', 'Invoice')
    tenant = Tenant.objects.create(
        name='Duplicate subscription checkout',
        slug='duplicate-subscription-checkout',
    )
    invoice_ids = [
        Invoice.objects.create(
            tenant=tenant,
            amount=Decimal('100.00'),
            currency='RUB',
            status='pending',
            purchase_type='subscription',
            checkout_client_key=uuid.uuid4(),
            provider_idempotency_key=str(uuid.uuid4()),
            checkout_payload_hash=character * 64,
            checkout_state='provider_created',
            yookassa_payment_id=f'pay_duplicate_subscription_{index}',
        ).pk
        for index, character in enumerate(('a', 'b'), start=1)
    ]
    legacy_tenant = Tenant.objects.create(
        name='Legacy pending subscription',
        slug='legacy-pending-subscription',
    )
    legacy_invoice_id = Invoice.objects.create(
        tenant=legacy_tenant,
        amount=Decimal('100.00'),
        currency='RUB',
        status='pending',
        purchase_type='subscription',
        checkout_state='legacy',
        yookassa_payment_id='pay_legacy_pending_subscription',
    ).pk
    mixed_tenant = Tenant.objects.create(
        name='Mixed manual and modern subscription',
        slug='mixed-subscription-checkout',
    )
    mixed_modern_id = Invoice.objects.create(
        tenant=mixed_tenant,
        amount=Decimal('100.00'),
        currency='RUB',
        status='pending',
        purchase_type='subscription',
        checkout_client_key=uuid.uuid4(),
        provider_idempotency_key=str(uuid.uuid4()),
        checkout_payload_hash='c' * 64,
        checkout_state='provider_pending',
    ).pk
    mixed_manual_id = Invoice.objects.create(
        tenant=mixed_tenant,
        amount=Decimal('100.00'),
        currency='RUB',
        status='pending',
        purchase_type='subscription',
        checkout_state='manual_review',
    ).pk
    single_tenant = Tenant.objects.create(
        name='Single modern subscription',
        slug='single-modern-subscription-checkout',
    )
    single_modern_id = Invoice.objects.create(
        tenant=single_tenant,
        amount=Decimal('100.00'),
        currency='RUB',
        status='pending',
        purchase_type='subscription',
        checkout_client_key=uuid.uuid4(),
        provider_idempotency_key=str(uuid.uuid4()),
        checkout_payload_hash='d' * 64,
        checkout_state='provider_pending',
    ).pk
    refund_tenant = Tenant.objects.create(
        name='Historical refund review',
        slug='historical-refund-review',
    )
    historical_refund_id = Invoice.objects.create(
        tenant=refund_tenant,
        amount=Decimal('100.00'),
        currency='RUB',
        status='manual_review',
        purchase_type='subscription',
        checkout_state='manual_review',
        paid_at=timezone.now() - timedelta(days=40),
        refund_review_required=True,
    ).pk
    post_refund_checkout_id = Invoice.objects.create(
        tenant=refund_tenant,
        amount=Decimal('100.00'),
        currency='RUB',
        status='pending',
        purchase_type='subscription',
        checkout_client_key=uuid.uuid4(),
        provider_idempotency_key=str(uuid.uuid4()),
        checkout_payload_hash='e' * 64,
        checkout_state='provider_pending',
    ).pk
    unpaid_refund_tenant = Tenant.objects.create(
        name='Unpaid refund review',
        slug='unpaid-refund-review',
    )
    unpaid_refund_review_id = Invoice.objects.create(
        tenant=unpaid_refund_tenant,
        amount=Decimal('100.00'),
        currency='RUB',
        status='manual_review',
        purchase_type='subscription',
        checkout_state='manual_review',
        refund_review_required=True,
    ).pk
    checkout_next_to_unpaid_refund_id = Invoice.objects.create(
        tenant=unpaid_refund_tenant,
        amount=Decimal('100.00'),
        currency='RUB',
        status='pending',
        purchase_type='subscription',
        checkout_client_key=uuid.uuid4(),
        provider_idempotency_key=str(uuid.uuid4()),
        checkout_payload_hash='f' * 64,
        checkout_state='provider_pending',
    ).pk

    try:
        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        new_apps = executor.loader.project_state(migrate_to).apps
        MigratedInvoice = new_apps.get_model('billing', 'Invoice')
        migrated = list(
            MigratedInvoice.objects.filter(pk__in=invoice_ids).order_by('pk')
        )

        assert [invoice.status for invoice in migrated] == [
            'manual_review',
            'manual_review',
        ]
        assert [invoice.checkout_state for invoice in migrated] == [
            'manual_review',
            'manual_review',
        ]
        assert all(
            'ручная проверка' in invoice.checkout_last_error
            for invoice in migrated
        )
        legacy_invoice = MigratedInvoice.objects.get(pk=legacy_invoice_id)
        assert legacy_invoice.status == 'manual_review'
        assert legacy_invoice.checkout_state == 'manual_review'
        assert 'ручная проверка' in legacy_invoice.checkout_last_error
        mixed = list(
            MigratedInvoice.objects.filter(
                pk__in=(mixed_modern_id, mixed_manual_id),
            ).order_by('pk')
        )
        assert [invoice.status for invoice in mixed] == [
            'manual_review',
            'manual_review',
        ]
        assert [invoice.checkout_state for invoice in mixed] == [
            'manual_review',
            'manual_review',
        ]
        single = MigratedInvoice.objects.get(pk=single_modern_id)
        assert single.status == 'pending'
        assert single.checkout_state == 'provider_pending'
        historical_refund = MigratedInvoice.objects.get(pk=historical_refund_id)
        assert historical_refund.status == 'manual_review'
        assert historical_refund.refund_review_required is True
        post_refund_checkout = MigratedInvoice.objects.get(
            pk=post_refund_checkout_id,
        )
        assert post_refund_checkout.status == 'pending'
        assert post_refund_checkout.checkout_state == 'provider_pending'
        unpaid_refund_rows = list(
            MigratedInvoice.objects.filter(
                pk__in=(
                    unpaid_refund_review_id,
                    checkout_next_to_unpaid_refund_id,
                ),
            ).order_by('pk')
        )
        assert [invoice.status for invoice in unpaid_refund_rows] == [
            'manual_review',
            'manual_review',
        ]
        assert all(
            invoice.checkout_state == 'manual_review'
            for invoice in unpaid_refund_rows
        )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
