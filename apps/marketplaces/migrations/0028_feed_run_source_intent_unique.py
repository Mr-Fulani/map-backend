from django.db import migrations, models


SOURCE_INTENT_INDEX = 'uniq_mkt_feed_source_intent'


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('marketplaces', '0027_feed_intent_due_concurrent_index'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        'DROP INDEX CONCURRENTLY IF EXISTS '
                        f'"{SOURCE_INTENT_INDEX}"'
                    ),
                    reverse_sql=migrations.RunSQL.noop,
                ),
                migrations.RunSQL(
                    sql=(
                        'CREATE UNIQUE INDEX CONCURRENTLY '
                        f'"{SOURCE_INTENT_INDEX}" '
                        'ON "marketplaces_marketplacefeedrun" '
                        '("account_id", "source_intent_revision") '
                        'WHERE "source_intent_revision" IS NOT NULL'
                    ),
                    reverse_sql=(
                        'DROP INDEX CONCURRENTLY IF EXISTS '
                        f'"{SOURCE_INTENT_INDEX}"'
                    ),
                ),
            ],
            state_operations=[
                migrations.AddConstraint(
                    model_name='marketplacefeedrun',
                    constraint=models.UniqueConstraint(
                        condition=models.Q(source_intent_revision__isnull=False),
                        fields=('account', 'source_intent_revision'),
                        name=SOURCE_INTENT_INDEX,
                    ),
                ),
            ],
        ),
    ]
