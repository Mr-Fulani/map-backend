import django.db.models.deletion
from django.db import migrations, models


def backfill_pending_ai_topup_entitlements(apps, schema_editor):
    """Freeze legacy payable top-ups or quarantine ambiguous purchases."""
    invoice_model = apps.get_model('billing', 'Invoice')
    package_model = apps.get_model('billing', 'AICreditPackage')
    database_alias = schema_editor.connection.alias
    invoices = (
        invoice_model.objects.using(database_alias)
        .filter(
            status='pending',
            purchase_type='ai_topup',
        )
        .exclude(yookassa_payment_id='')
        .iterator(chunk_size=500)
    )
    for invoice in invoices:
        if invoice.entitlement_snapshot:
            continue
        metadata = invoice.metadata if isinstance(invoice.metadata, dict) else {}
        raw_package_id = metadata.get('package_id')
        package_id = None
        if not isinstance(raw_package_id, bool):
            try:
                package_id = int(raw_package_id)
            except (TypeError, ValueError):
                pass
        package = (
            package_model.objects.using(database_alias)
            .filter(pk=package_id)
            .first()
            if package_id and package_id > 0
            else None
        )
        if (
            package is not None
            and invoice.currency == 'RUB'
            and invoice.amount == package.price_rub
            and package.updated_at <= invoice.created_at
            and package.price_rub.is_finite()
            and package.price_rub > 0
            and package.credits.is_finite()
            and package.credits > 0
        ):
            invoice_model.objects.using(database_alias).filter(pk=invoice.pk).update(
                entitlement_snapshot={
                    'schema': 1,
                    'purchase_type': 'ai_topup',
                    'amount': str(invoice.amount),
                    'currency': invoice.currency,
                    'package': {
                        'id': package.pk,
                        'name': package.name,
                        'credits': str(package.credits),
                        'price_rub': str(package.price_rub),
                    },
                    'legacy_pending_backfill': True,
                },
            )
            continue

        invoice_model.objects.using(database_alias).filter(pk=invoice.pk).update(
            status='manual_review',
            checkout_state='manual_review',
            checkout_last_error=(
                'Legacy AI top-up не удалось безопасно связать с неизменяемым '
                'пакетом; требуется ручная проверка.'
            ),
            next_reconciliation_at=None,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0012_harden_yookassa_webhooks'),
    ]

    operations = [
        migrations.AddField(
            model_name='subscription',
            name='billing_version',
            field=models.PositiveBigIntegerField(
                default=0,
                verbose_name='Версия биллингового состояния',
            ),
        ),
        migrations.AddField(
            model_name='invoice',
            name='checkout_attempt_count',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='invoice',
            name='checkout_client_key',
            field=models.UUIDField(
                blank=True,
                editable=False,
                null=True,
                verbose_name='Ключ идемпотентности клиента',
            ),
        ),
        migrations.AddField(
            model_name='invoice',
            name='checkout_confirmation_url',
            field=models.URLField(
                blank=True,
                editable=False,
                max_length=2048,
                verbose_name='Confirmation URL YooKassa',
            ),
        ),
        migrations.AddField(
            model_name='invoice',
            name='checkout_first_attempt_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='invoice',
            name='checkout_last_attempt_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='invoice',
            name='checkout_last_error',
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name='invoice',
            name='checkout_payload_hash',
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=64,
                verbose_name='Хеш checkout payload',
            ),
        ),
        migrations.AddField(
            model_name='invoice',
            name='checkout_return_url',
            field=models.URLField(
                blank=True,
                editable=False,
                max_length=2048,
                verbose_name='Return URL checkout',
            ),
        ),
        migrations.AddField(
            model_name='invoice',
            name='checkout_state',
            field=models.CharField(
                choices=[
                    ('legacy', 'Legacy счёт'),
                    ('intent_created', 'Намерение создано'),
                    ('provider_pending', 'Результат провайдера неизвестен'),
                    ('provider_created', 'Платёж у провайдера создан'),
                    ('manual_review', 'Требует ручной проверки'),
                ],
                default='legacy',
                max_length=24,
                verbose_name='Состояние checkout intent',
            ),
        ),
        migrations.AddField(
            model_name='invoice',
            name='entitlement_plan',
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='purchase_invoices',
                to='billing.plan',
                verbose_name='Купленный тариф',
            ),
        ),
        migrations.AddField(
            model_name='invoice',
            name='entitlement_snapshot',
            field=models.JSONField(
                blank=True,
                default=dict,
                editable=False,
                verbose_name='Неизменяемый снимок покупки',
            ),
        ),
        migrations.AddField(
            model_name='invoice',
            name='expected_subscription_version',
            field=models.PositiveBigIntegerField(
                blank=True,
                editable=False,
                null=True,
                verbose_name='Ожидаемая версия подписки',
            ),
        ),
        migrations.AddField(
            model_name='invoice',
            name='last_reconciliation_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='invoice',
            name='next_reconciliation_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='invoice',
            name='provider_idempotency_key',
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=64,
                verbose_name='Ключ идемпотентности YooKassa',
            ),
        ),
        migrations.AddField(
            model_name='invoice',
            name='reconciliation_attempts',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='billingwebhookevent',
            name='last_reconciliation_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='billingwebhookevent',
            name='next_reconciliation_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='billingwebhookevent',
            name='reconciliation_attempts',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.RunPython(
            backfill_pending_ai_topup_entitlements,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddIndex(
            model_name='invoice',
            index=models.Index(
                fields=['checkout_state', 'next_reconciliation_at'],
                name='billing_inv_reconcile_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='billingwebhookevent',
            index=models.Index(
                fields=['decision', 'next_reconciliation_at'],
                name='billing_wh_reconcile_idx',
            ),
        ),
        migrations.AddConstraint(
            model_name='invoice',
            constraint=models.UniqueConstraint(
                condition=models.Q(checkout_client_key__isnull=False),
                fields=('tenant', 'checkout_client_key'),
                name='unique_tenant_checkout_client_key',
            ),
        ),
        migrations.AddConstraint(
            model_name='invoice',
            constraint=models.UniqueConstraint(
                condition=~models.Q(provider_idempotency_key=''),
                fields=('provider_idempotency_key',),
                name='unique_provider_idempotency_key',
            ),
        ),
    ]
