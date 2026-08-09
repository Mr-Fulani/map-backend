from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0015_webhook_delivery_claims'),
    ]

    operations = [
        # Keep this DDL in a migration after the claim backfill. PostgreSQL
        # cannot ALTER this table while the preceding data migration still has
        # deferred FK trigger events in the same transaction.
        migrations.AddConstraint(
            model_name='webhookdelivery',
            constraint=models.CheckConstraint(
                check=(
                    models.Q(
                        status__in=['queued', 'delivering'],
                        claim_token__isnull=False,
                        claimed_at__isnull=False,
                    )
                    | models.Q(
                        status__in=['pending', 'retry', 'delivered', 'failed'],
                        claim_token__isnull=True,
                        claimed_at__isnull=True,
                    )
                ),
                name='webhook_delivery_claim_state_valid',
            ),
        ),
        migrations.AddIndex(
            model_name='webhookdelivery',
            index=models.Index(
                fields=['status', 'claimed_at'],
                name='wh_delivery_claim_idx',
            ),
        ),
    ]
