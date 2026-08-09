import uuid

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def _fernet():
    keys = getattr(settings, 'FIELD_ENCRYPTION_KEYS', None) or [
        getattr(settings, 'FIELD_ENCRYPTION_KEY', ''),
    ]
    keys = [key.strip() for key in keys if key and key.strip()]
    if not keys:
        raise RuntimeError(
            'FIELD_ENCRYPTION_KEY(S) обязателен для шифрования webhook secrets.',
        )
    return MultiFernet([Fernet(key.encode()) for key in keys])


def encrypt_webhook_secrets(apps, schema_editor):
    WebhookEndpoint = apps.get_model('tenants', 'WebhookEndpoint')
    cipher = _fernet()
    for endpoint in WebhookEndpoint.objects.all().iterator():
        endpoint.secret_encrypted = cipher.encrypt(endpoint.secret.encode())
        endpoint.save(update_fields=['secret_encrypted'])


def decrypt_webhook_secrets(apps, schema_editor):
    WebhookEndpoint = apps.get_model('tenants', 'WebhookEndpoint')
    cipher = _fernet()
    for endpoint in WebhookEndpoint.objects.all().iterator():
        endpoint.secret = cipher.decrypt(bytes(endpoint.secret_encrypted)).decode()
        endpoint.save(update_fields=['secret'])


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0011_tenant_ai_credit_limit_override'),
    ]

    operations = [
        migrations.AlterField(
            model_name='webhookendpoint',
            name='secret',
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.AddField(
            model_name='webhookendpoint',
            name='secret_encrypted',
            field=models.BinaryField(null=True),
        ),
        migrations.RunPython(encrypt_webhook_secrets, decrypt_webhook_secrets),
        migrations.RemoveField(
            model_name='webhookendpoint',
            name='secret',
        ),
        migrations.AlterField(
            model_name='webhookendpoint',
            name='secret_encrypted',
            field=models.BinaryField(),
        ),
        migrations.AddField(
            model_name='webhookendpoint',
            name='deleted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='WebhookEvent',
            fields=[
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('event_type', models.CharField(choices=[
                    ('listing.published', 'listing.published'),
                    ('listing.rejected', 'listing.rejected'),
                    ('listing.archived', 'listing.archived'),
                    ('import.completed', 'import.completed'),
                    ('import.failed', 'import.failed'),
                    ('billing.payment_success', 'billing.payment_success'),
                    ('billing.payment_failed', 'billing.payment_failed'),
                ], max_length=64)),
                ('payload', models.JSONField(default=dict)),
                ('idempotency_key', models.CharField(blank=True, max_length=200)),
                ('tenant', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='webhook_events_outbox',
                    to='tenants.tenant',
                )),
            ],
            options={
                'verbose_name': 'Исходящее webhook-событие',
                'verbose_name_plural': 'Исходящие webhook-события',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='WebhookDelivery',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('endpoint_url', models.URLField(max_length=500)),
                ('status', models.CharField(choices=[
                    ('pending', 'Ожидает'), ('delivering', 'Отправляется'),
                    ('retry', 'Повтор'), ('delivered', 'Доставлено'),
                    ('failed', 'Не доставлено'),
                ], default='pending', max_length=20)),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('max_attempts', models.PositiveSmallIntegerField(default=8)),
                ('next_attempt_at', models.DateTimeField(blank=True, null=True)),
                ('last_attempt_at', models.DateTimeField(blank=True, null=True)),
                ('delivered_at', models.DateTimeField(blank=True, null=True)),
                ('response_status', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('response_body', models.TextField(blank=True)),
                ('last_error', models.TextField(blank=True)),
                ('endpoint', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='deliveries',
                    to='tenants.webhookendpoint',
                )),
                ('event', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='deliveries',
                    to='tenants.webhookevent',
                )),
            ],
            options={
                'verbose_name': 'Доставка webhook',
                'verbose_name_plural': 'Доставки webhook',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='webhookevent',
            constraint=models.UniqueConstraint(
                condition=~models.Q(idempotency_key=''),
                fields=('tenant', 'idempotency_key'),
                name='unique_tenant_webhook_event_idempotency_key',
            ),
        ),
        migrations.AddIndex(
            model_name='webhookevent',
            index=models.Index(fields=['tenant', '-created_at'], name='wh_event_tenant_created_idx'),
        ),
        migrations.AddConstraint(
            model_name='webhookdelivery',
            constraint=models.UniqueConstraint(
                fields=('event', 'endpoint'),
                name='unique_webhook_event_endpoint_delivery',
            ),
        ),
        migrations.AddIndex(
            model_name='webhookdelivery',
            index=models.Index(fields=['status', 'next_attempt_at'], name='wh_delivery_status_due_idx'),
        ),
        migrations.AddIndex(
            model_name='webhookdelivery',
            index=models.Index(fields=['endpoint', '-created_at'], name='wh_delivery_endpoint_idx'),
        ),
    ]
