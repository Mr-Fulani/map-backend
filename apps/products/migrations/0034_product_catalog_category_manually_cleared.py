from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0033_current_catalog_description_facts'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='catalog_category_manually_cleared',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Не применять к товару автоматический маппинг категории '
                    'после ручного снятия.'
                ),
                verbose_name='Категория каталога снята вручную',
            ),
        ),
    ]
