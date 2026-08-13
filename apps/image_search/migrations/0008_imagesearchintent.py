import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('image_search', '0007_retention_indexes'),
        ('tenants', '0011_tenant_ai_credit_limit_override'),
    ]

    operations = [
        migrations.CreateModel(
            name='ImageSearchIntent',
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
                (
                    'operation',
                    models.CharField(
                        choices=[
                            ('single', 'Одиночный поиск'),
                            ('bulk', 'Массовый поиск'),
                        ],
                        max_length=20,
                    ),
                ),
                ('idempotency_key', models.UUIDField(default=uuid.uuid4, editable=False)),
                ('request_fingerprint', models.CharField(editable=False, max_length=64)),
                ('request_payload', models.JSONField(default=dict, editable=False)),
                (
                    'tenant',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='image_search_intents',
                        to='tenants.tenant',
                    ),
                ),
            ],
            options={
                'indexes': [
                    models.Index(
                        fields=['tenant', '-created_at'],
                        name='img_intent_tenant_created_idx',
                    ),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('tenant', 'operation', 'idempotency_key'),
                        name='unique_tenant_image_search_intent',
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name='imagesearchtask',
            name='intent',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='tasks',
                to='image_search.imagesearchintent',
            ),
        ),
    ]
