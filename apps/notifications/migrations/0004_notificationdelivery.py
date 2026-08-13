import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0003_backfill_tenant_notification_settings'),
        ('tenants', '0016_webhook_delivery_claim_constraint'),
    ]

    operations = [
        migrations.CreateModel(
            name='NotificationDelivery',
            fields=[
                (
                    'id',
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ('event_key', models.CharField(max_length=200)),
                (
                    'channel',
                    models.CharField(
                        choices=[('email', 'Email'), ('telegram', 'Telegram')],
                        max_length=20,
                    ),
                ),
                ('payload_fingerprint', models.CharField(max_length=64)),
                (
                    'status',
                    models.CharField(
                        choices=[
                            ('pending', 'Ожидает отправки'),
                            ('sending', 'Отправляется'),
                            ('sent', 'Отправлено'),
                            ('skipped', 'Пропущено'),
                            ('failed', 'Не отправлено'),
                            ('outcome_uncertain', 'Требует сверки'),
                        ],
                        db_index=True,
                        default='pending',
                        max_length=24,
                    ),
                ),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('claimed_at', models.DateTimeField(blank=True, null=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('error_code', models.CharField(blank=True, max_length=80)),
                ('reconciliation_action', models.CharField(blank=True, max_length=20)),
                ('reconciliation_note', models.TextField(blank=True)),
                ('reconciled_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'tenant',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='notification_deliveries',
                        to='tenants.tenant',
                    ),
                ),
            ],
            options={'ordering': ['created_at', 'id']},
        ),
        migrations.AddConstraint(
            model_name='notificationdelivery',
            constraint=models.UniqueConstraint(
                fields=('tenant', 'event_key', 'channel'),
                name='uniq_notification_event_channel',
            ),
        ),
        migrations.AddIndex(
            model_name='notificationdelivery',
            index=models.Index(
                fields=['status', 'updated_at'],
                name='notif_delivery_status_idx',
            ),
        ),
    ]
