from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketplaces', '0036_ozon_category_policy'),
    ]

    operations = [
        migrations.AddField(
            model_name='ozonofferdraft',
            name='autofill',
            field=models.JSONField(
                blank=True,
                default=dict,
                verbose_name='Безопасное автозаполнение Ozon',
            ),
        ),
    ]
