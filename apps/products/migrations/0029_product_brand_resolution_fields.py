from django.db import migrations, models


def backfill_brand_resolution(apps, schema_editor):
    Product = apps.get_model('products', 'Product')
    Product.objects.exclude(brand='').update(
        brand_resolution_status='source',
        brand_confidence=0.8,
        brand_source_id='legacy',
        brand_needs_review=False,
    )


def reset_brand_resolution(apps, schema_editor):
    Product = apps.get_model('products', 'Product')
    Product.objects.update(
        brand_resolution_status='unknown',
        brand_confidence=0.0,
        brand_source_id='',
        brand_needs_review=False,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0028_nullable_inherited_category_margin'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='brand_confidence',
            field=models.FloatField(default=0.0, verbose_name='Уверенность бренда'),
        ),
        migrations.AddField(
            model_name='product',
            name='brand_needs_review',
            field=models.BooleanField(
                db_index=True,
                default=False,
                verbose_name='Бренд требует проверки',
            ),
        ),
        migrations.AddField(
            model_name='product',
            name='brand_resolution_status',
            field=models.CharField(
                choices=[
                    ('unknown', 'Не определён'),
                    ('source', 'Получен из источника'),
                    ('catalog', 'Найден в каталоге'),
                    ('manual', 'Подтверждён вручную'),
                    ('ambiguous', 'Есть конфликт'),
                ],
                db_index=True,
                default='unknown',
                max_length=20,
                verbose_name='Статус определения бренда',
            ),
        ),
        migrations.AddField(
            model_name='product',
            name='brand_source_id',
            field=models.CharField(
                blank=True,
                max_length=50,
                verbose_name='Источник бренда',
            ),
        ),
        migrations.RunPython(backfill_brand_resolution, reset_brand_resolution),
    ]
