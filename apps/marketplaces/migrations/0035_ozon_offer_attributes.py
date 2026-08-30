import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('marketplaces', '0034_ozon_offer_draft'),
    ]

    operations = [
        migrations.CreateModel(
            name='OzonAttributeValueSnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('description_category_id', models.PositiveBigIntegerField(verbose_name='ID категории Ozon')),
                ('type_id', models.PositiveBigIntegerField(verbose_name='ID типа товара Ozon')),
                ('attribute_id', models.PositiveBigIntegerField(verbose_name='ID характеристики Ozon')),
                ('language', models.CharField(choices=[('DEFAULT', 'По умолчанию'), ('RU', 'Русский'), ('EN', 'Английский'), ('TR', 'Турецкий'), ('ZH_HANS', 'Китайский')], default='DEFAULT', max_length=10, verbose_name='Язык схемы')),
                ('query', models.CharField(max_length=120, verbose_name='Строка поиска')),
                ('attribute_schema_hash', models.CharField(max_length=64, verbose_name='Версия схемы характеристики')),
                ('schema_hash', models.CharField(max_length=64, verbose_name='SHA-256 нормализованных значений')),
                ('values', models.JSONField(default=list, verbose_name='Значения справочника')),
                ('value_count', models.PositiveSmallIntegerField(verbose_name='Количество значений')),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ozon_attribute_value_snapshots', to='marketplaces.marketplaceaccount', verbose_name='Аккаунт Ozon')),
            ],
            options={
                'verbose_name': 'Снимок значений характеристики Ozon',
                'verbose_name_plural': 'Снимки значений характеристик Ozon',
                'indexes': [
                    models.Index(fields=['account', 'description_category_id', 'type_id', 'attribute_id', '-updated_at'], name='mkt_oz_value_latest_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(fields=('account', 'description_category_id', 'type_id', 'attribute_id', 'language', 'query', 'attribute_schema_hash', 'schema_hash'), name='mkt_oz_value_revision_uniq'),
                ],
            },
        ),
        migrations.AddField(
            model_name='ozonofferdraft',
            name='attribute_schema_revision',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='ozonofferdraft',
            name='attributes',
            field=models.JSONField(blank=True, default=list, verbose_name='Подготовленные характеристики Ozon'),
        ),
    ]
