import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0038_review_queue_indexes'),
        ('tenants', '0011_tenant_ai_credit_limit_override'),
    ]

    operations = [
        migrations.AddField(
            model_name='productbulkactionjob',
            name='idempotency_key',
            field=models.UUIDField(
                blank=True,
                editable=False,
                null=True,
                verbose_name='Ключ идемпотентности',
            ),
        ),
        migrations.AddField(
            model_name='productbulkactionjob',
            name='request_fingerprint',
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=64,
                verbose_name='Отпечаток входного запроса',
            ),
        ),
        migrations.AddField(
            model_name='productbulkactionjob',
            name='request_payload',
            field=models.JSONField(
                blank=True,
                default=dict,
                editable=False,
                verbose_name='Канонический входной запрос',
            ),
        ),
        migrations.CreateModel(
            name='ProductParseIntent',
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
                ('idempotency_key', models.UUIDField(default=uuid.uuid4, editable=False)),
                ('request_fingerprint', models.CharField(editable=False, max_length=64)),
                ('request_payload', models.JSONField(default=dict, editable=False)),
                ('job_ids', models.JSONField(default=list, editable=False)),
                ('generate_after', models.BooleanField(default=False, editable=False)),
                (
                    'primary_job',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='primary_for_intents',
                        to='products.productparsejob',
                    ),
                ),
                (
                    'product',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='parse_intents',
                        to='products.product',
                    ),
                ),
                (
                    'tenant',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='product_parse_intents',
                        to='tenants.tenant',
                    ),
                ),
            ],
            options={
                'indexes': [
                    models.Index(
                        fields=['tenant', '-created_at'],
                        name='prod_parse_intent_created_idx',
                    ),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('tenant', 'idempotency_key'),
                        name='unique_tenant_product_parse_intent',
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name='productparsejob',
            name='ingress_intent',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='jobs',
                to='products.productparseintent',
                verbose_name='Ingress intent',
            ),
        ),
        migrations.AddConstraint(
            model_name='productbulkactionjob',
            constraint=models.UniqueConstraint(
                condition=models.Q(idempotency_key__isnull=False),
                fields=('tenant', 'idempotency_key'),
                name='unique_tenant_product_bulk_intent',
            ),
        ),
    ]
