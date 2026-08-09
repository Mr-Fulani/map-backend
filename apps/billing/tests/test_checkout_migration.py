"""Migration coverage for legacy payable AI top-ups."""

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
