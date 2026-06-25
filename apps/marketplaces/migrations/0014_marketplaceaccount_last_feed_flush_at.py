from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketplaces', '0013_listing_status_archiving'),
    ]

    operations = [
        migrations.AddField(
            model_name='marketplaceaccount',
            name='last_feed_flush_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name='Последняя автозагрузка фида',
            ),
        ),
    ]
