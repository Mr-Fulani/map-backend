from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('marketplaces', '0032_ozon_account_profile'),
    ]

    operations = [
        migrations.CreateModel(
            name='OzonCategoryTreeSnapshot',
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
                    'language',
                    models.CharField(
                        choices=[
                            ('DEFAULT', 'По умолчанию'),
                            ('RU', 'Русский'),
                            ('EN', 'Английский'),
                            ('TR', 'Турецкий'),
                            ('ZH_HANS', 'Китайский'),
                        ],
                        default='DEFAULT',
                        max_length=10,
                        verbose_name='Язык схемы',
                    ),
                ),
                (
                    'schema_hash',
                    models.CharField(
                        max_length=64,
                        verbose_name='SHA-256 нормализованной схемы',
                    ),
                ),
                (
                    'tree',
                    models.JSONField(verbose_name='Нормализованное дерево категорий'),
                ),
                (
                    'node_count',
                    models.PositiveIntegerField(verbose_name='Количество узлов'),
                ),
                (
                    'active_type_count',
                    models.PositiveIntegerField(
                        verbose_name='Количество доступных типов товаров',
                    ),
                ),
                (
                    'account',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='ozon_category_tree_snapshots',
                        to='marketplaces.marketplaceaccount',
                        verbose_name='Аккаунт Ozon',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Снимок дерева категорий Ozon',
                'verbose_name_plural': 'Снимки дерева категорий Ozon',
                'indexes': [
                    models.Index(
                        fields=['account', 'language', '-updated_at'],
                        name='mkt_oz_tree_latest_idx',
                    ),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('account', 'language', 'schema_hash'),
                        name='mkt_oz_tree_revision_uniq',
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name='OzonCategoryAttributeSnapshot',
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
                    'description_category_id',
                    models.PositiveBigIntegerField(verbose_name='ID категории Ozon'),
                ),
                (
                    'type_id',
                    models.PositiveBigIntegerField(verbose_name='ID типа товара Ozon'),
                ),
                (
                    'language',
                    models.CharField(
                        choices=[
                            ('DEFAULT', 'По умолчанию'),
                            ('RU', 'Русский'),
                            ('EN', 'Английский'),
                            ('TR', 'Турецкий'),
                            ('ZH_HANS', 'Китайский'),
                        ],
                        default='DEFAULT',
                        max_length=10,
                        verbose_name='Язык схемы',
                    ),
                ),
                (
                    'schema_hash',
                    models.CharField(
                        max_length=64,
                        verbose_name='SHA-256 нормализованной схемы',
                    ),
                ),
                (
                    'attributes',
                    models.JSONField(verbose_name='Нормализованные характеристики'),
                ),
                (
                    'attribute_count',
                    models.PositiveIntegerField(
                        verbose_name='Количество характеристик',
                    ),
                ),
                (
                    'required_attribute_count',
                    models.PositiveIntegerField(
                        verbose_name='Количество обязательных характеристик',
                    ),
                ),
                (
                    'account',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='ozon_category_attribute_snapshots',
                        to='marketplaces.marketplaceaccount',
                        verbose_name='Аккаунт Ozon',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Снимок характеристик категории Ozon',
                'verbose_name_plural': 'Снимки характеристик категорий Ozon',
                'indexes': [
                    models.Index(
                        fields=[
                            'account', 'description_category_id', 'type_id',
                            'language', '-updated_at',
                        ],
                        name='mkt_oz_attr_latest_idx',
                    ),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        fields=(
                            'account', 'description_category_id', 'type_id',
                            'language', 'schema_hash',
                        ),
                        name='mkt_oz_attr_revision_uniq',
                    ),
                    models.CheckConstraint(
                        condition=models.Q(description_category_id__gt=0),
                        name='mkt_oz_attr_category_pos',
                    ),
                    models.CheckConstraint(
                        condition=models.Q(type_id__gt=0),
                        name='mkt_oz_attr_type_pos',
                    ),
                ],
            },
        ),
    ]
