from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0024_alter_productbulkactionjob_action'),
    ]

    operations = [
        migrations.AlterField(
            model_name='productimage',
            name='url_source',
            field=models.URLField(blank=True, max_length=2000, verbose_name='Исходный URL изображения'),
        ),
    ]
