import django.db.models
from django.db import migrations, models


ACTIVE_CHECKOUT_STATES = (
    'intent_created',
    'provider_pending',
    'provider_created',
)


def quarantine_duplicate_subscription_checkouts(apps, schema_editor):
    """Fail closed when historical rows may represent multiple payments."""
    invoice_model = apps.get_model('billing', 'Invoice')
    database_alias = schema_editor.connection.alias

    # 0013 introduced checkout_state with ``legacy`` as its historical default.
    # A pending subscription row may already identify a real provider payment,
    # but it has neither a durable payload snapshot nor a safe reconciliation
    # contract. Quarantine every such row before the modern uniqueness
    # constraint is installed; selecting one as reusable could create a second
    # charge for the same tenant.
    invoice_model.objects.using(database_alias).filter(
        purchase_type='subscription',
        status='pending',
        checkout_state='legacy',
    ).update(
        status='manual_review',
        checkout_state='manual_review',
        checkout_last_error=(
            'Legacy checkout подписки не имеет доказуемого финального статуса; '
            'требуется ручная проверка.'
        ),
        next_reconciliation_at=None,
    )

    # Normalize any partially migrated/manual state so the service-side
    # unresolved-invoice guard cannot mistake it for a retryable pending intent.
    invoice_model.objects.using(database_alias).filter(
        purchase_type='subscription',
        status='pending',
        checkout_state='manual_review',
    ).update(
        status='manual_review',
        checkout_last_error=(
            'Checkout подписки требует ручной проверки до новой оплаты.'
        ),
        next_reconciliation_at=None,
    )

    # A modern pending intent next to any already-unresolved manual invoice is
    # just as ambiguous as two modern intents: either provider payment may win.
    # Quarantine the modern side too, so rollout never leaves an apparently
    # retryable charge next to a financial obligation awaiting an operator.
    manual_review_tenant_ids = (
        invoice_model.objects.using(database_alias)
        .filter(
            purchase_type='subscription',
            status='manual_review',
            paid_at__isnull=True,
        )
        .values('tenant_id')
    )
    invoice_model.objects.using(database_alias).filter(
        tenant_id__in=manual_review_tenant_ids,
        purchase_type='subscription',
        status='pending',
        checkout_state__in=ACTIVE_CHECKOUT_STATES,
    ).update(
        status='manual_review',
        checkout_state='manual_review',
        checkout_last_error=(
            'Активный checkout подписки сосуществует с '
            'неразрешённым платежом; требуется ручная проверка.'
        ),
        next_reconciliation_at=None,
    )

    duplicate_tenants = (
        invoice_model.objects.using(database_alias)
        .filter(
            purchase_type='subscription',
            status='pending',
            checkout_state__in=ACTIVE_CHECKOUT_STATES,
        )
        .values('tenant_id')
        .annotate(row_count=django.db.models.Count('pk'))
        .filter(row_count__gt=1)
    )
    # There may already be multiple provider-side payments. Selecting a winner
    # automatically could grant the wrong entitlement, so quarantine every
    # conflicting intent in one set-based update. Keeping tenant IDs as a
    # subquery avoids materializing an unbounded Python list / SQL IN payload.
    invoice_model.objects.using(database_alias).filter(
        tenant_id__in=duplicate_tenants.values('tenant_id'),
        purchase_type='subscription',
        status='pending',
        checkout_state__in=ACTIVE_CHECKOUT_STATES,
    ).update(
        status='manual_review',
        checkout_state='manual_review',
        checkout_last_error=(
            'Несколько активных checkout подписки обнаружены миграцией; '
            'требуется ручная проверка.'
        ),
        next_reconciliation_at=None,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0016_billing_outbox_dead_letter'),
    ]

    operations = [
        migrations.RunPython(
            quarantine_duplicate_subscription_checkouts,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name='invoice',
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    purchase_type='subscription',
                    status='pending',
                    checkout_state__in=ACTIVE_CHECKOUT_STATES,
                ),
                fields=('tenant',),
                name='uniq_active_subscription_checkout',
            ),
        ),
    ]
