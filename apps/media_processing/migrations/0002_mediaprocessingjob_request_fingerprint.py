from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('media_processing', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='mediaprocessingjob',
            name='request_fingerprint',
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=64,
                verbose_name='Отпечаток входного запроса',
            ),
        ),
    ]
