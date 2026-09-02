from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('marketplaces', '0040_ozon_publication_operations'),
    ]

    operations = [
        migrations.AddField(
            model_name='ozonoperation',
            name='last_reconciled_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='ozonoperation',
            name='next_reconcile_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='ozonoperation',
            name='reconcile_count',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddConstraint(
            model_name='ozonoperation',
            constraint=models.CheckConstraint(
                condition=models.Q(('reconcile_count__lte', 100)),
                name='mkt_oz_operation_reconcile_bound',
            ),
        ),
    ]
