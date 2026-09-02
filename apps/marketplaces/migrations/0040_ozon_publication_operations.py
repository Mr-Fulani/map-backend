import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('marketplaces', '0038_ozon_offer_margin'),
    ]

    operations = [
        migrations.AddField(
            model_name='ozonaccountprofile',
            name='product_write_enabled',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Account-scoped kill switch. Включается только после '
                    'отдельного write-canary и не влияет на Avito.'
                ),
                verbose_name='Разрешена запись товаров Ozon',
            ),
        ),
        migrations.AddField(
            model_name='ozonofferdraft',
            name='last_provider_sync_at',
            field=models.DateTimeField(
                blank=True,
                editable=False,
                null=True,
                verbose_name='Последняя сверка с Ozon',
            ),
        ),
        migrations.AddField(
            model_name='ozonofferdraft',
            name='moderation_status',
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=100,
                verbose_name='Статус модерации Ozon',
            ),
        ),
        migrations.AddField(
            model_name='ozonofferdraft',
            name='provider_errors',
            field=models.JSONField(
                blank=True,
                default=list,
                editable=False,
                verbose_name='Последние ошибки Ozon',
            ),
        ),
        migrations.AddField(
            model_name='ozonofferdraft',
            name='provider_product_id',
            field=models.PositiveBigIntegerField(
                blank=True,
                editable=False,
                null=True,
                verbose_name='Product ID Ozon',
            ),
        ),
        migrations.AddField(
            model_name='ozonofferdraft',
            name='provider_sku',
            field=models.PositiveBigIntegerField(
                blank=True,
                editable=False,
                null=True,
                verbose_name='SKU Ozon',
            ),
        ),
        migrations.AddField(
            model_name='ozonofferdraft',
            name='provider_status',
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=100,
                verbose_name='Статус товара в Ozon',
            ),
        ),
        migrations.AddField(
            model_name='ozonofferdraft',
            name='publication_status',
            field=models.CharField(
                default='local_draft',
                editable=False,
                max_length=32,
                verbose_name='Состояние публикации Ozon',
            ),
        ),
        migrations.CreateModel(
            name='OzonOperation',
            fields=[
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('kind', models.CharField(choices=[('product_import', 'Создание или обновление товара'), ('price_update', 'Обновление цены'), ('stock_update', 'Обновление остатка'), ('archive', 'Архивирование товара')], max_length=32)),
                ('state', models.CharField(choices=[('queued', 'В очереди'), ('sending', 'Отправляется'), ('outcome_unknown', 'Результат неизвестен'), ('reconciling', 'Сверяется с Ozon'), ('succeeded', 'Завершено'), ('partial', 'Завершено частично'), ('failed', 'Ошибка'), ('manual_review', 'Требуется проверка')], default='queued', max_length=32)),
                ('idempotency_key', models.CharField(max_length=100)),
                ('request_sha256', models.CharField(max_length=64)),
                ('request_summary', models.JSONField(blank=True, default=dict)),
                ('response_summary', models.JSONField(blank=True, default=dict)),
                ('errors', models.JSONField(blank=True, default=list)),
                ('provider_task_id', models.CharField(blank=True, max_length=100)),
                ('attempt_count', models.PositiveSmallIntegerField(default=0)),
                ('last_attempt_at', models.DateTimeField(blank=True, null=True)),
                ('retry_after_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ozon_operations', to='marketplaces.marketplaceaccount', verbose_name='Аккаунт Ozon')),
                ('offer', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='operations', to='marketplaces.ozonofferdraft', verbose_name='Черновик товара Ozon')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ozon_operations', to='tenants.tenant', verbose_name='Тенант')),
            ],
            options={
                'verbose_name': 'Операция Ozon',
                'verbose_name_plural': 'Операции Ozon',
                'indexes': [
                    models.Index(fields=['account', 'state', '-created_at'], name='mkt_oz_operation_state_idx'),
                    models.Index(fields=['offer', 'kind', '-created_at'], name='mkt_oz_operation_offer_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(fields=('account', 'idempotency_key'), name='mkt_oz_operation_idempotency_uniq'),
                    models.CheckConstraint(condition=models.Q(('attempt_count__lte', 100)), name='mkt_oz_operation_attempt_bound'),
                ],
            },
        ),
    ]
