from django.db import migrations, models


class Migration(migrations.Migration):
    """Добавляет поддержку web-режима публикации (Playwright) в MarketplaceAccount."""

    dependencies = [
        ('marketplaces', '0009_listing_status_queued'),
    ]

    operations = [
        migrations.AddField(
            model_name='marketplaceaccount',
            name='publish_method',
            field=models.CharField(
                choices=[('autoload', 'Autoload API'), ('web', 'Веб-режим (Playwright)')],
                default='autoload',
                max_length=20,
                verbose_name='Метод публикации',
            ),
        ),
        migrations.AddField(
            model_name='marketplaceaccount',
            name='web_login_enc',
            field=models.BinaryField(
                blank=True, null=True,
                verbose_name='Логин Avito для веб-режима (зашифровано)',
            ),
        ),
        migrations.AddField(
            model_name='marketplaceaccount',
            name='web_password_enc',
            field=models.BinaryField(
                blank=True, null=True,
                verbose_name='Пароль Avito для веб-режима (зашифровано)',
            ),
        ),
    ]
