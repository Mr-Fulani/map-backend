from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('marketplaces', '0041_ozon_operation_reconciliation'),
    ]

    operations = [
        migrations.AddField(
            model_name='ozonofferdraft',
            name='last_price_sync_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='ozonofferdraft',
            name='last_stock_sync_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='ozonofferdraft',
            name='last_stock_warehouse_id',
            field=models.CharField(blank=True, editable=False, max_length=100, verbose_name='Склад последней синхронизации остатка Ozon'),
        ),
        migrations.AddField(
            model_name='ozonofferdraft',
            name='last_synced_price',
            field=models.DecimalField(blank=True, decimal_places=2, editable=False, max_digits=12, null=True, verbose_name='Последняя подтверждённая цена Ozon'),
        ),
        migrations.AddField(
            model_name='ozonofferdraft',
            name='last_synced_stock',
            field=models.PositiveIntegerField(blank=True, editable=False, null=True, verbose_name='Последний подтверждённый остаток Ozon'),
        ),
    ]
