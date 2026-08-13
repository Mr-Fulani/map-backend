import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
        ('tenants', '0016_webhook_delivery_claim_constraint'),
    ]

    operations = [
        migrations.CreateModel(
            name='PaidIngressIntent',
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
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('operation', models.SlugField(max_length=80)),
                ('idempotency_key', models.UUIDField(default=uuid.uuid4, editable=False)),
                ('request_fingerprint', models.CharField(editable=False, max_length=64)),
                ('raw_payload_fingerprint', models.CharField(editable=False, max_length=64)),
                ('request_payload', models.JSONField(default=dict, editable=False)),
                ('resource_type', models.CharField(editable=False, max_length=80)),
                ('resource_id', models.CharField(editable=False, max_length=80)),
                ('result_type', models.CharField(blank=True, editable=False, max_length=80)),
                ('result_id', models.CharField(blank=True, editable=False, max_length=80)),
                ('result_metadata', models.JSONField(blank=True, default=dict, editable=False)),
                (
                    'dispatch',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='paid_ingress_intents',
                        to='core.backgroundjobdispatch',
                    ),
                ),
                (
                    'tenant',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='paid_ingress_intents',
                        to='tenants.tenant',
                    ),
                ),
            ],
            options={
                'indexes': [
                    models.Index(
                        fields=['tenant', '-created_at'],
                        name='paid_intent_tenant_created_idx',
                    ),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('tenant', 'operation', 'idempotency_key'),
                        name='uniq_tenant_paid_ingress',
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name='TenantDailyPaidUsage',
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
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('scope', models.SlugField(max_length=80)),
                ('usage_date', models.DateField()),
                ('units', models.PositiveIntegerField(default=0)),
                (
                    'tenant',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='daily_paid_usage',
                        to='tenants.tenant',
                    ),
                ),
            ],
            options={
                'indexes': [
                    models.Index(
                        fields=['scope', 'usage_date'],
                        name='paid_usage_scope_date_idx',
                    ),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('tenant', 'scope', 'usage_date'),
                        name='uniq_tenant_daily_paid_usage',
                    ),
                    models.CheckConstraint(
                        condition=models.Q(('units__gte', 0)),
                        name='daily_paid_usage_nonnegative',
                    ),
                ],
            },
        ),
    ]
