from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('marketplaces', '0045_ozon_provider_barcodes')]

    operations = [
        migrations.AddField(
            model_name='ozonaccountprofile', name='automation_health',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='ozonaccountprofile', name='commerce_checked_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='ozonaccountprofile', name='commerce_cursor',
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='ozonaccountprofile', name='orders_checked_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='ozonaccountprofile', name='reconciliation_checked_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='ozonaccountprofile', name='reconciliation_cursor',
            field=models.UUIDField(blank=True, null=True),
        ),
    ]
