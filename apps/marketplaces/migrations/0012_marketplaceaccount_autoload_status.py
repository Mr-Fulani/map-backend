from django.db import migrations, models


class Migration(migrations.Migration):
    """Сохраняет последний известный статус Avito Автозагрузки на аккаунте."""

    dependencies = [
        ('marketplaces', '0011_alter_listing_ad_type_default'),
    ]

    operations = [
        migrations.AddField(
            model_name='marketplaceaccount',
            name='autoload_active',
            field=models.BooleanField(blank=True, null=True, verbose_name='Автозагрузка Avito активна'),
        ),
        migrations.AddField(
            model_name='marketplaceaccount',
            name='autoload_checked_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Статус Автозагрузки проверен'),
        ),
    ]
