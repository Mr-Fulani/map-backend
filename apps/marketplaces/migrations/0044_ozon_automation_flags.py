from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('marketplaces', '0043_ozon_fbs_postings')]
    operations = [
        migrations.AddField(
            model_name='ozonaccountprofile', name='commerce_auto_sync_enabled',
            field=models.BooleanField(default=False, verbose_name='Автосинхронизация цен и остатков Ozon'),
        ),
        migrations.AddField(
            model_name='ozonaccountprofile', name='orders_auto_sync_enabled',
            field=models.BooleanField(default=False, verbose_name='Автосинхронизация FBS-заказов Ozon'),
        ),
    ]
