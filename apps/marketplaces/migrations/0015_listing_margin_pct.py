from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketplaces', '0014_marketplaceaccount_last_feed_flush_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='listing',
            name='margin_pct',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=5,
                null=True,
                verbose_name='Наценка листинга, % (override категории)',
            ),
        ),
    ]
