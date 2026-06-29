from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0026_product_sync_excluded'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenantcatalogcategory',
            name='default_margin_pct',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('0.00'),
                max_digits=5,
                verbose_name='Наценка по умолчанию, %',
            ),
        ),
    ]
