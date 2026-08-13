from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0037_alter_productcatalogclassification_source'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='productcatalogclassification',
            index=models.Index(
                fields=['tenant', 'needs_review', 'review_status', '-updated_at', 'id'],
                name='prd_class_review_queue_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='vehiclefitment',
            index=models.Index(
                fields=['tenant', 'needs_review', 'review_status', '-updated_at', 'id'],
                name='vehicle_review_queue_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='productenrichmentfact',
            index=models.Index(
                fields=['tenant', 'needs_review', 'review_status', '-updated_at', 'id'],
                name='prd_fact_review_queue_idx',
            ),
        ),
    ]
