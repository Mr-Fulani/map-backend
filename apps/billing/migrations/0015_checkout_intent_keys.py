import django.db.models.deletion
from django.db import migrations, models


def backfill_canonical_checkout_keys(apps, schema_editor):
    invoice_model = apps.get_model('billing', 'Invoice')
    key_model = apps.get_model('billing', 'CheckoutIntentKey')
    database_alias = schema_editor.connection.alias
    batch = []
    invoices = (
        invoice_model.objects.using(database_alias)
        .filter(checkout_client_key__isnull=False)
        .only(
            'pk', 'tenant_id', 'checkout_client_key',
            'checkout_payload_hash',
        )
        .iterator(chunk_size=1000)
    )
    for invoice in invoices:
        batch.append(key_model(
            tenant_id=invoice.tenant_id,
            invoice_id=invoice.pk,
            client_key=invoice.checkout_client_key,
            checkout_payload_hash=invoice.checkout_payload_hash,
        ))
        if len(batch) == 1000:
            key_model.objects.using(database_alias).bulk_create(batch)
            batch = []
    if batch:
        key_model.objects.using(database_alias).bulk_create(batch)


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0014_billing_outbox_and_checkout_dedup'),
    ]

    operations = [
        migrations.CreateModel(
            name='CheckoutIntentKey',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'created_at',
                    models.DateTimeField(auto_now_add=True, verbose_name='Создано'),
                ),
                (
                    'updated_at',
                    models.DateTimeField(auto_now=True, verbose_name='Обновлено'),
                ),
                ('client_key', models.UUIDField(editable=False)),
                (
                    'checkout_payload_hash',
                    models.CharField(editable=False, max_length=64),
                ),
                (
                    'invoice',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='client_keys',
                        to='billing.invoice',
                    ),
                ),
                (
                    'tenant',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='checkout_intent_keys',
                        to='tenants.tenant',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Ключ checkout intent',
                'verbose_name_plural': 'Ключи checkout intent',
                'ordering': ['-created_at'],
            },
        ),
        migrations.RunPython(
            backfill_canonical_checkout_keys,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name='checkoutintentkey',
            constraint=models.UniqueConstraint(
                fields=('tenant', 'client_key'),
                name='unique_tenant_checkout_intent_key',
            ),
        ),
    ]
