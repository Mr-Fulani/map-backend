from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('marketplaces', '0046_ozon_automation_cursors')]

    operations = [
        migrations.AddField(
            model_name='ozonaccountprofile', name='health_monitor_checked_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='ozonaccountprofile', name='health_monitor_state',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
