from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketplaces', '0025_marketplace_feed_endpoint'),
    ]

    operations = [
        migrations.AddField(
            model_name='marketplaceaccount',
            name='feed_intent_dispatched_revision',
            field=models.PositiveBigIntegerField(
                default=0,
                editable=False,
                verbose_name='Последняя отправленная ревизия фида',
            ),
        ),
        migrations.AddField(
            model_name='marketplaceaccount',
            name='feed_intent_due_at',
            field=models.DateTimeField(
                blank=True,
                editable=False,
                null=True,
                verbose_name='Следующая отправка ревизии фида',
            ),
        ),
        migrations.AddField(
            model_name='marketplaceaccount',
            name='feed_intent_revision',
            field=models.PositiveBigIntegerField(
                default=0,
                editable=False,
                verbose_name='Ревизия требуемого состояния фида',
            ),
        ),
        migrations.AddField(
            model_name='marketplacefeedendpoint',
            name='source_intent_revision',
            field=models.PositiveBigIntegerField(
                default=0,
                editable=False,
                verbose_name='Текущая желаемая ревизия фида',
            ),
        ),
        migrations.AddField(
            model_name='marketplacefeedrun',
            name='source_intent_revision',
            field=models.PositiveBigIntegerField(
                blank=True,
                editable=False,
                null=True,
                verbose_name='Исходная ревизия намерения фида',
            ),
        ),
        migrations.AddConstraint(
            model_name='marketplaceaccount',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    feed_intent_dispatched_revision__lte=models.F(
                        'feed_intent_revision',
                    ),
                ),
                name='mkt_acct_intent_order',
            ),
        ),
    ]
