from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('marketplaces', '0031_live_private_successor_guard'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='marketplaceaccount',
            options={
                'verbose_name': 'Аккаунт маркетплейса',
                'verbose_name_plural': 'Аккаунты маркетплейсов',
            },
        ),
        migrations.AlterField(
            model_name='marketplaceaccount',
            name='external_id',
            field=models.CharField(
                max_length=100,
                verbose_name='ID аккаунта у маркетплейса',
            ),
        ),
        migrations.AlterField(
            model_name='marketplaceaccount',
            name='marketplace',
            field=models.CharField(
                choices=[('avito', 'Avito'), ('ozon', 'Ozon')],
                default='avito',
                max_length=50,
                verbose_name='Маркетплейс',
            ),
        ),
        migrations.CreateModel(
            name='OzonAccountProfile',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'created_at',
                    models.DateTimeField(auto_now_add=True, verbose_name='Создано'),
                ),
                (
                    'updated_at',
                    models.DateTimeField(auto_now=True, verbose_name='Обновлено'),
                ),
                (
                    'connection_status',
                    models.CharField(
                        choices=[
                            ('connected', 'Подключён'),
                            ('warehouse_missing', 'Склад не найден'),
                            (
                                'warehouse_selection_required',
                                'Требуется выбрать склад',
                            ),
                        ],
                        default='connected',
                        max_length=40,
                        verbose_name='Статус подключения',
                    ),
                ),
                (
                    'company_name',
                    models.CharField(blank=True, max_length=300, verbose_name='Компания'),
                ),
                (
                    'seller_name',
                    models.CharField(blank=True, max_length=300, verbose_name='Продавец'),
                ),
                (
                    'currency',
                    models.CharField(blank=True, max_length=10, verbose_name='Валюта'),
                ),
                ('roles', models.JSONField(default=list, verbose_name='Роли API-ключа')),
                (
                    'api_methods',
                    models.JSONField(default=list, verbose_name='Методы API-ключа'),
                ),
                (
                    'api_key_expires_at',
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        verbose_name='API-ключ истекает',
                    ),
                ),
                (
                    'warehouse_count',
                    models.PositiveIntegerField(default=0, verbose_name='Количество складов'),
                ),
                (
                    'selected_warehouse_id',
                    models.CharField(
                        blank=True,
                        max_length=100,
                        verbose_name='Выбранный склад Ozon',
                    ),
                ),
                (
                    'selected_warehouse_name',
                    models.CharField(
                        blank=True,
                        max_length=300,
                        verbose_name='Название выбранного склада',
                    ),
                ),
                ('last_checked_at', models.DateTimeField(verbose_name='Подключение проверено')),
                (
                    'account',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='ozon_profile',
                        to='marketplaces.marketplaceaccount',
                        verbose_name='Аккаунт Ozon',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Профиль аккаунта Ozon',
                'verbose_name_plural': 'Профили аккаунтов Ozon',
            },
        ),
        migrations.AddConstraint(
            model_name='marketplaceaccount',
            constraint=models.UniqueConstraint(
                condition=models.Q(('marketplace', 'ozon')),
                fields=('marketplace', 'external_id'),
                name='mkt_acct_ozon_identity_uniq',
            ),
        ),
    ]
