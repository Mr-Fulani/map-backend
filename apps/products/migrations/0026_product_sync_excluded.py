from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0025_increase_url_source_max_length'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='sync_excluded',
            field=models.BooleanField(default=False, verbose_name='Исключён из синхронизации'),
        ),
    ]
