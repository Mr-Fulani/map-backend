from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('marketplaces', '0020_listing_status_lifecycle_expand'),
    ]

    operations = [
        AddIndexConcurrently(
            model_name='listing',
            index=models.Index(
                condition=(
                    models.Q(
                        deleted_at__isnull=True,
                        external_id__isnull=False,
                        next_status_check_at__isnull=False,
                    )
                    & ~models.Q(external_id='')
                ),
                fields=['account', 'status', 'next_status_check_at', 'id'],
                name='mkt_lst_acct_stat_due',
            ),
        ),
    ]
