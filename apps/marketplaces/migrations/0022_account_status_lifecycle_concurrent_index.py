from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('marketplaces', '0021_status_lifecycle_concurrent_indexes'),
    ]

    operations = [
        AddIndexConcurrently(
            model_name='marketplaceaccount',
            index=models.Index(
                condition=models.Q(
                    deleted_at__isnull=True,
                    is_active=True,
                    status_batch_due_at__isnull=False,
                ),
                fields=['marketplace', 'status_batch_due_at', 'id'],
                name='mkt_acct_provider_due',
            ),
        ),
    ]
