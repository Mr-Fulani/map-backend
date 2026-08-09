import uuid

from django.db import migrations, models


LEGACY_ERROR_MESSAGE = (
    'legacy_error: Предыдущая ошибка доставки удалена при ротации безопасного формата.'
)


def initialize_claims_and_scrub_errors(apps, schema_editor):
    """Make existing in-flight rows recoverable and remove historic URL-bearing errors."""
    WebhookDelivery = apps.get_model('tenants', 'WebhookDelivery')
    WebhookDelivery.objects.exclude(last_error='').update(
        last_error=LEGACY_ERROR_MESSAGE,
    )

    delivering = list(
        WebhookDelivery.objects.filter(status='delivering').only(
            'pk', 'claim_token', 'claimed_at', 'last_attempt_at', 'updated_at',
        ),
    )
    for delivery in delivering:
        delivery.claim_token = uuid.uuid4()
        delivery.claimed_at = delivery.last_attempt_at or delivery.updated_at
    if delivering:
        WebhookDelivery.objects.bulk_update(
            delivering,
            ['claim_token', 'claimed_at'],
        )


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0014_webhook_endpoint_guards'),
    ]

    operations = [
        migrations.AddField(
            model_name='webhookdelivery',
            name='claim_token',
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='webhookdelivery',
            name='claimed_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AlterField(
            model_name='webhookdelivery',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Ожидает'),
                    ('queued', 'В очереди'),
                    ('delivering', 'Отправляется'),
                    ('retry', 'Повтор'),
                    ('delivered', 'Доставлено'),
                    ('failed', 'Не доставлено'),
                ],
                default='pending',
                max_length=20,
            ),
        ),
        migrations.RunPython(
            initialize_claims_and_scrub_errors,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
