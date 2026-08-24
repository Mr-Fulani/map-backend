from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('marketplaces', '0023_marketplace_feed_run'),
    ]

    operations = [
        AddIndexConcurrently(
            model_name='listing',
            index=models.Index(
                condition=models.Q(
                    deleted_at__isnull=True,
                    external_id__isnull=True,
                    feed_run__isnull=False,
                ),
                fields=['feed_run', 'status', 'id'],
                name='mkt_lst_feed_pending',
            ),
        ),
    ]
