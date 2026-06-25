from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('datasources', '0002_alter_datasourceconnection_options_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='datasourceconnection',
            name='content_hash',
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text='Для CSV/Excel — хэш содержимого, чтобы ловить повторную загрузку того же файла.',
                max_length=64,
                verbose_name='SHA-256 загруженного файла',
            ),
        ),
    ]
