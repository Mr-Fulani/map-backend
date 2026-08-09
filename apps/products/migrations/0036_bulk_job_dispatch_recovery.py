from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0035_product_soft_delete'),
    ]

    operations = [
        migrations.AddField(
            model_name='productbulkactionjob',
            name='last_dispatched_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name='Последняя постановка batch в очередь',
            ),
        ),
        migrations.AddIndex(
            model_name='productbulkactionjob',
            index=models.Index(
                fields=['status', 'next_batch_at'],
                name='prod_bulk_status_due_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='productbulkactionjob',
            index=models.Index(
                fields=['status', 'last_dispatched_at'],
                name='prod_bulk_dispatch_idx',
            ),
        ),
    ]
