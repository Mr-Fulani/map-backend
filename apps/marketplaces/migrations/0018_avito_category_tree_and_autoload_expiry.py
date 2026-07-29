from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('marketplaces', '0017_avitoaccountstatus'),
    ]

    operations = [
        migrations.AddField(
            model_name='marketplaceaccount',
            name='autoload_subscription_ends_at',
            field=models.DateField(
                blank=True,
                help_text=(
                    'Заполняется вручную, когда Avito Autoload API не возвращает срок подписки. '
                    'Тариф API категории «Транспорт», если доступен, имеет приоритет.'
                ),
                null=True,
                verbose_name='Дата окончания Автозагрузки',
            ),
        ),
        migrations.CreateModel(
            name='AvitoCategoryTreeSnapshot',
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
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('domain_slug', models.CharField(max_length=50, unique=True, verbose_name='Домен каталога')),
                ('root_name', models.CharField(max_length=200, verbose_name='Корень Avito')),
                ('tree', models.JSONField(default=list, verbose_name='Дерево')),
                ('checksum', models.CharField(blank=True, max_length=64, verbose_name='Контрольная сумма')),
                (
                    'status',
                    models.CharField(
                        choices=[('ready', 'Готово'), ('error', 'Ошибка')],
                        default='ready',
                        max_length=20,
                        verbose_name='Статус',
                    ),
                ),
                ('node_count', models.PositiveIntegerField(default=0, verbose_name='Количество узлов')),
                ('change_count', models.PositiveIntegerField(default=0, verbose_name='Изменённых путей')),
                ('fetched_at', models.DateTimeField(blank=True, null=True, verbose_name='Получено из Avito')),
                ('applied_at', models.DateTimeField(blank=True, null=True, verbose_name='Применено к тенантам')),
                ('last_error', models.CharField(blank=True, max_length=500, verbose_name='Последняя ошибка')),
                ('metadata', models.JSONField(default=dict, verbose_name='Метаданные синхронизации')),
                (
                    'source_account',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='category_tree_snapshots',
                        to='marketplaces.marketplaceaccount',
                        verbose_name='Аккаунт-источник',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Снимок дерева категорий Avito',
                'verbose_name_plural': 'Снимки дерева категорий Avito',
            },
        ),
    ]
