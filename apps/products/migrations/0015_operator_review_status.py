from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('products', '0014_source_provenance_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='productcatalogclassification',
            name='review_status',
            field=models.CharField(
                choices=[
                    ('pending', 'Ожидает проверки'),
                    ('approved', 'Одобрено'),
                    ('rejected', 'Отклонено'),
                ],
                default='pending',
                max_length=20,
                verbose_name='Статус проверки',
            ),
        ),
        migrations.AddField(
            model_name='productcatalogclassification',
            name='reviewed_at',
            field=models.DateTimeField(null=True, blank=True, verbose_name='Дата проверки'),
        ),
        migrations.AddField(
            model_name='productcatalogclassification',
            name='reviewed_by',
            field=models.ForeignKey(
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='reviewed_catalog_classifications',
                to=settings.AUTH_USER_MODEL,
                verbose_name='Проверил',
            ),
        ),
        migrations.AddField(
            model_name='productenrichmentfact',
            name='review_status',
            field=models.CharField(
                choices=[
                    ('pending', 'Ожидает проверки'),
                    ('approved', 'Одобрено'),
                    ('rejected', 'Отклонено'),
                ],
                default='pending',
                max_length=20,
                verbose_name='Статус проверки',
            ),
        ),
        migrations.AddField(
            model_name='productenrichmentfact',
            name='reviewed_at',
            field=models.DateTimeField(null=True, blank=True, verbose_name='Дата проверки'),
        ),
        migrations.AddField(
            model_name='productenrichmentfact',
            name='reviewed_by',
            field=models.ForeignKey(
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='reviewed_enrichment_facts',
                to=settings.AUTH_USER_MODEL,
                verbose_name='Проверил',
            ),
        ),
        migrations.AddField(
            model_name='vehiclefitment',
            name='review_status',
            field=models.CharField(
                choices=[
                    ('pending', 'Ожидает проверки'),
                    ('approved', 'Одобрено'),
                    ('rejected', 'Отклонено'),
                ],
                default='pending',
                max_length=20,
                verbose_name='Статус проверки',
            ),
        ),
        migrations.AddField(
            model_name='vehiclefitment',
            name='reviewed_at',
            field=models.DateTimeField(null=True, blank=True, verbose_name='Дата проверки'),
        ),
        migrations.AddField(
            model_name='vehiclefitment',
            name='reviewed_by',
            field=models.ForeignKey(
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='reviewed_vehicle_fitments',
                to=settings.AUTH_USER_MODEL,
                verbose_name='Проверил',
            ),
        ),
    ]
