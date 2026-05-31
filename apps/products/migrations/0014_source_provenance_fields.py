from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0013_tenantcatalogcategory_default_image_s3_key_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='productenrichmentfact',
            name='last_seen_at',
            field=models.DateTimeField(null=True, blank=True, verbose_name='Последний раз найдено'),
        ),
        migrations.AddField(
            model_name='productenrichmentfact',
            name='source_url',
            field=models.URLField(blank=True, verbose_name='URL источника'),
        ),
        migrations.AddField(
            model_name='vehiclefitment',
            name='last_seen_at',
            field=models.DateTimeField(null=True, blank=True, verbose_name='Последний раз найдено'),
        ),
        migrations.AddField(
            model_name='vehiclefitment',
            name='source_url',
            field=models.URLField(blank=True, verbose_name='URL источника'),
        ),
    ]
