import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('marketplaces', '0010_marketplace_account_web_mode'),
    ]

    operations = [
        migrations.CreateModel(
            name='BrowserSession',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('browser_type', models.CharField(
                    choices=[('chromium', 'Chromium'), ('firefox', 'Firefox'), ('webkit', 'WebKit')],
                    max_length=20, verbose_name='Тип браузера',
                )),
                ('profile_dir', models.CharField(max_length=500, verbose_name='Путь к профилю браузера')),
                ('cookies_enc', models.BinaryField(blank=True, null=True, verbose_name='Куки (зашифровано)')),
                ('fingerprint', models.JSONField(default=dict, verbose_name='Fingerprint')),
                ('proxy_url_enc', models.BinaryField(blank=True, null=True, verbose_name='Прокси (зашифровано)')),
                ('session_valid', models.BooleanField(default=False, verbose_name='Сессия активна')),
                ('sms_pending', models.BooleanField(default=False, verbose_name='Ожидает код подтверждения')),
                ('last_login_at', models.DateTimeField(blank=True, null=True, verbose_name='Последний вход')),
                ('account', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='browser_session',
                    to='marketplaces.marketplaceaccount',
                    verbose_name='Аккаунт',
                )),
            ],
            options={
                'verbose_name': 'Браузерная сессия',
                'verbose_name_plural': 'Браузерные сессии',
            },
        ),
    ]
