import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('marketplaces', '0035_ozon_offer_attributes'),
        ('tenants', '0016_webhook_delivery_claim_constraint'),
    ]

    operations = [
        migrations.CreateModel(
            name='OzonCategoryPolicy',
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
                (
                    'description_category_id',
                    models.PositiveBigIntegerField(verbose_name='ID категории Ozon'),
                ),
                (
                    'type_id',
                    models.PositiveBigIntegerField(
                        blank=True,
                        null=True,
                        verbose_name='ID типа товара Ozon',
                    ),
                ),
                (
                    'enabled_override',
                    models.BooleanField(
                        blank=True,
                        help_text=(
                            'Пустое значение наследует настройку ближайшей '
                            'родительской категории.'
                        ),
                        null=True,
                        verbose_name='Собственное включение категории',
                    ),
                ),
                (
                    'margin_pct',
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text=(
                            'Пустое значение наследует наценку ближайшей '
                            'родительской категории.'
                        ),
                        max_digits=5,
                        null=True,
                        verbose_name='Наценка Ozon, %',
                    ),
                ),
                (
                    'category_path',
                    models.CharField(
                        blank=True,
                        max_length=1000,
                        verbose_name='Последний известный путь категории Ozon',
                    ),
                ),
                (
                    'node_name',
                    models.CharField(
                        blank=True,
                        max_length=500,
                        verbose_name='Последнее известное название узла Ozon',
                    ),
                ),
                (
                    'tree_revision',
                    models.CharField(max_length=64, verbose_name='Ревизия дерева Ozon'),
                ),
                (
                    'account',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='ozon_category_policies',
                        to='marketplaces.marketplaceaccount',
                        verbose_name='Аккаунт Ozon',
                    ),
                ),
                (
                    'tenant',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='ozon_category_policies',
                        to='tenants.tenant',
                        verbose_name='Тенант',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Правило категории Ozon',
                'verbose_name_plural': 'Правила категорий Ozon',
                'indexes': [
                    models.Index(
                        fields=['tenant', 'account', 'description_category_id'],
                        name='mkt_oz_policy_lookup_idx',
                    ),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        condition=models.Q(type_id__isnull=True),
                        fields=('account', 'description_category_id'),
                        name='mkt_oz_policy_category_uniq',
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(type_id__isnull=False),
                        fields=('account', 'description_category_id', 'type_id'),
                        name='mkt_oz_policy_type_uniq',
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(enabled_override__isnull=False)
                            | models.Q(margin_pct__isnull=False)
                        ),
                        name='mkt_oz_policy_has_override',
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(margin_pct__isnull=True)
                            | models.Q(margin_pct__gte=-100)
                        ),
                        name='mkt_oz_policy_margin_min',
                    ),
                ],
            },
        ),
    ]
