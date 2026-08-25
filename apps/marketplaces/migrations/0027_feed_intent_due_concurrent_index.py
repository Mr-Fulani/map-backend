from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('marketplaces', '0026_feed_intent_expand'),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                'DROP INDEX CONCURRENTLY IF EXISTS '
                '"mkt_acct_feed_intent_due"'
            ),
            reverse_sql=migrations.RunSQL.noop,
        ),
        AddIndexConcurrently(
            model_name='marketplaceaccount',
            index=models.Index(
                condition=models.Q(
                    deleted_at__isnull=True,
                    feed_intent_due_at__isnull=False,
                    is_active=True,
                ),
                fields=['marketplace', 'feed_intent_due_at', 'id'],
                name='mkt_acct_feed_intent_due',
            ),
        ),
    ]
