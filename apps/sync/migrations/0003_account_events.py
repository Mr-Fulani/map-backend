import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('marketplaces', '0046_ozon_automation_cursors'),
        ('sync', '0002_alter_synclog_options_alter_synclog_created_at_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='synclog', name='account',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='sync_logs', to='marketplaces.marketplaceaccount', verbose_name='Кабинет',
            ),
        ),
        migrations.AddField(
            model_name='synclog', name='event_key',
            field=models.CharField(blank=True, max_length=160, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name='synclog', name='event_type',
            field=models.CharField(choices=[
                ('datasource_import', 'Импорт данных'), ('description_gen', 'Генерация описания'),
                ('listing_publish', 'Публикация'), ('listing_update', 'Обновление'),
                ('listing_price_update', 'Обновление цены'), ('listing_stock_update', 'Обновление остатка'),
                ('orders_sync', 'Синхронизация заказов'), ('listing_unpublish', 'Снятие с публикации'),
                ('listing_delete', 'Удаление'), ('listing_error', 'Ошибка листинга'),
                ('moderation', 'Модерация'), ('billing_event', 'Биллинг'),
                ('anti_ban_trigger', 'Анти-бан'), ('rate_limit_hit', 'Rate limit'),
            ], max_length=30, verbose_name='Тип события'),
        ),
        migrations.AddIndex(
            model_name='synclog',
            index=models.Index(fields=['tenant', 'account', '-created_at'], name='sync_tenant_account_time_idx'),
        ),
    ]
