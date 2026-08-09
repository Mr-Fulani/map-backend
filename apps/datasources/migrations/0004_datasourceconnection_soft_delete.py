from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('datasources', '0003_datasourceconnection_content_hash'),
    ]

    operations = [
        migrations.AddField(
            model_name='datasourceconnection',
            name='deleted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
