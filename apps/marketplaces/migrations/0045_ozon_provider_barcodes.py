from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('marketplaces', '0044_ozon_automation_flags')]
    operations = [
        migrations.AddField(
            model_name='ozonofferdraft',
            name='barcode_generation_error',
            field=models.CharField(blank=True, editable=False, max_length=500,
                                   verbose_name='Ошибка создания штрихкода Ozon'),
        ),
        migrations.AddField(
            model_name='ozonofferdraft',
            name='barcode_generation_requested_at',
            field=models.DateTimeField(blank=True, editable=False, null=True,
                                       verbose_name='Создание штрихкода Ozon запрошено'),
        ),
        migrations.AddField(
            model_name='ozonofferdraft',
            name='barcode_generation_status',
            field=models.CharField(choices=[
                ('not_requested', 'Не запрошен'), ('requesting', 'Запрашивается'),
                ('requested', 'Запрошен, ожидается в Ozon'), ('ready', 'Готов'),
                ('failed', 'Ошибка'), ('outcome_unknown', 'Результат неизвестен'),
            ], default='not_requested', editable=False, max_length=32,
                verbose_name='Состояние создания штрихкода Ozon'),
        ),
        migrations.AddField(
            model_name='ozonofferdraft',
            name='provider_barcodes',
            field=models.JSONField(blank=True, default=list, editable=False,
                                   verbose_name='Штрихкоды товара в Ozon'),
        ),
    ]
