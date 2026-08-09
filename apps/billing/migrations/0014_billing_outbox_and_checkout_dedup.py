import uuid

import django.db.models
import django.db.models.deletion
from django.db import migrations, models


ACTIVE_CHECKOUT_STATES = (
    'intent_created',
    'provider_pending',
    'provider_created',
)


def quarantine_duplicate_active_checkout_intents(apps, schema_editor):
    """Fail closed before adding the active-payload uniqueness invariant."""
    invoice_model = apps.get_model('billing', 'Invoice')
    database_alias = schema_editor.connection.alias
    duplicates = (
        invoice_model.objects.using(database_alias)
        .filter(
            status='pending',
            checkout_state__in=ACTIVE_CHECKOUT_STATES,
        )
        .exclude(checkout_payload_hash='')
        .values('tenant_id', 'checkout_payload_hash')
        .annotate(row_count=django.db.models.Count('pk'))
        .filter(row_count__gt=1)
    )
    for duplicate in list(duplicates):
        invoice_ids = list(
            invoice_model.objects.using(database_alias)
            .filter(
                tenant_id=duplicate['tenant_id'],
                checkout_payload_hash=duplicate['checkout_payload_hash'],
                status='pending',
                checkout_state__in=ACTIVE_CHECKOUT_STATES,
            )
            .order_by('created_at', 'pk')
            .values_list('pk', flat=True)
        )
        # Existing duplicates may already represent multiple provider payments;
        # choosing an arbitrary winner would risk silently fulfilling the wrong
        # one. Quarantine every member of the group for operator review.
        invoice_model.objects.using(database_alias).filter(
            pk__in=invoice_ids,
        ).update(
            status='manual_review',
            checkout_state='manual_review',
            checkout_last_error=(
                'Дублирующий активный checkout intent обнаружен миграцией; '
                'требуется ручная проверка.'
            ),
            next_reconciliation_at=None,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0013_durable_checkout_intents'),
    ]

    operations = [
        migrations.RunPython(
            quarantine_duplicate_active_checkout_intents,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name='invoice',
            constraint=models.UniqueConstraint(
                condition=(
                    models.Q(
                        status='pending',
                        checkout_state__in=ACTIVE_CHECKOUT_STATES,
                    )
                    & ~models.Q(checkout_payload_hash='')
                ),
                fields=('tenant', 'checkout_payload_hash'),
                name='uniq_active_checkout_payload',
            ),
        ),
        migrations.CreateModel(
            name='BillingOutboxEvent',
            fields=[
                (
                    'created_at',
                    models.DateTimeField(auto_now_add=True, verbose_name='Создано'),
                ),
                (
                    'updated_at',
                    models.DateTimeField(auto_now=True, verbose_name='Обновлено'),
                ),
                (
                    'id',
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    'event_type',
                    models.CharField(
                        choices=[
                            ('notification', 'Уведомление'),
                            (
                                'requeue_limit_reached',
                                'Повтор публикации после снятия лимита',
                            ),
                        ],
                        editable=False,
                        max_length=40,
                    ),
                ),
                (
                    'idempotency_key',
                    models.CharField(editable=False, max_length=200),
                ),
                ('payload', models.JSONField(default=dict, editable=False)),
                (
                    'status',
                    models.CharField(
                        choices=[
                            ('pending', 'Ожидает отправки'),
                            ('processing', 'Отправляется'),
                            ('dispatched', 'Отправлено брокеру'),
                        ],
                        default='pending',
                        max_length=20,
                    ),
                ),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('next_attempt_at', models.DateTimeField(blank=True, null=True)),
                (
                    'processing_token',
                    models.UUIDField(blank=True, editable=False, null=True),
                ),
                (
                    'processing_started_at',
                    models.DateTimeField(blank=True, null=True),
                ),
                ('dispatched_at', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.CharField(blank=True, max_length=500)),
                (
                    'invoice',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='outbox_events',
                        to='billing.invoice',
                    ),
                ),
                (
                    'tenant',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='billing_outbox_events',
                        to='tenants.tenant',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Событие billing outbox',
                'verbose_name_plural': 'События billing outbox',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='billingoutboxevent',
            constraint=models.UniqueConstraint(
                fields=('tenant', 'idempotency_key'),
                name='unique_tenant_billing_outbox_key',
            ),
        ),
        migrations.AddIndex(
            model_name='billingoutboxevent',
            index=models.Index(
                fields=['status', 'next_attempt_at'],
                name='billing_outbox_due_idx',
            ),
        ),
    ]
