from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0034_product_catalog_category_manually_cleared'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='deleted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
