from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('marketplaces', '0016_avitobrandcatalog'),
        ('tenants', '0010_rename_generic_mixed_domain_labels'),
    ]

    operations = [
        migrations.CreateModel(
            name='AvitoAccountStatus',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('connection_status', models.CharField(
                    choices=[
                        ('unknown', 'Не проверено'),
                        ('connected', 'Подключено'),
                        ('auth_error', 'Ошибка авторизации'),
                        ('unavailable', 'Avito временно недоступен'),
                    ],
                    default='unknown', max_length=20, verbose_name='Состояние подключения',
                )),
                ('autoload_status', models.CharField(
                    choices=[
                        ('unknown', 'Не проверено'),
                        ('enabled', 'Включена'),
                        ('disabled', 'Выключена'),
                        ('missing', 'Профиль отсутствует'),
                        ('forbidden', 'Нет доступа'),
                    ],
                    default='unknown', max_length=20, verbose_name='Состояние Автозагрузки',
                )),
                ('feed_configured', models.BooleanField(
                    blank=True, null=True, verbose_name='Фид MAP настроен',
                )),
                ('profile_checked_at', models.DateTimeField(
                    blank=True, null=True, verbose_name='Профиль проверен',
                )),
                ('tariff_status', models.CharField(
                    choices=[
                        ('unknown', 'Не проверено'),
                        ('active', 'Активен'),
                        ('inactive', 'Неактивен'),
                        ('not_found', 'Данные недоступны для аккаунта'),
                        ('unavailable', 'Avito временно недоступен'),
                    ],
                    default='unknown', max_length=20, verbose_name='Состояние тарифа',
                )),
                ('tariff_name', models.CharField(blank=True, max_length=200, verbose_name='Тариф')),
                ('tariff_started_at', models.DateTimeField(
                    blank=True, null=True, verbose_name='Тариф начался',
                )),
                ('tariff_ends_at', models.DateTimeField(
                    blank=True, null=True, verbose_name='Тариф заканчивается',
                )),
                ('tariff_price', models.DecimalField(
                    blank=True, decimal_places=2, max_digits=12,
                    null=True, verbose_name='Стоимость тарифа',
                )),
                ('placement_packages', models.JSONField(
                    default=list, verbose_name='Пакеты размещений',
                )),
                ('scheduled_tariff', models.JSONField(
                    default=dict, verbose_name='Следующий тариф',
                )),
                ('tariff_checked_at', models.DateTimeField(
                    blank=True, null=True, verbose_name='Тариф проверен',
                )),
                ('last_attempted_at', models.DateTimeField(
                    blank=True, null=True, verbose_name='Последняя попытка проверки',
                )),
                ('last_error_code', models.CharField(
                    blank=True, max_length=50, verbose_name='Код последней ошибки',
                )),
                ('last_error_message', models.CharField(
                    blank=True, max_length=500, verbose_name='Последняя ошибка',
                )),
                ('notification_state', models.JSONField(
                    default=dict, verbose_name='Отправленные пороги уведомлений',
                )),
                ('account', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='avito_status',
                    to='marketplaces.marketplaceaccount',
                    verbose_name='Аккаунт Avito',
                )),
                ('tenant', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='avito_account_statuses',
                    to='tenants.tenant',
                    verbose_name='Тенант',
                )),
            ],
            options={
                'verbose_name': 'Состояние Avito-аккаунта',
                'verbose_name_plural': 'Состояния Avito-аккаунтов',
                'indexes': [
                    models.Index(
                        fields=['tenant', 'tariff_status'],
                        name='mkt_avito_tenant_tariff_idx',
                    ),
                    models.Index(
                        fields=['tenant', 'autoload_status'],
                        name='mkt_avito_tenant_autoload_idx',
                    ),
                ],
            },
        ),
    ]
