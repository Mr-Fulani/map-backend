from django.db import migrations


def backfill_tenant_notification_settings(apps, schema_editor):
    TenantNotificationSettings = apps.get_model(
        'notifications',
        'TenantNotificationSettings',
    )
    TenantUser = apps.get_model('tenants', 'TenantUser')

    owners = (
        TenantUser.objects.filter(role='owner')
        .select_related('user')
        .order_by('tenant_id', 'pk')
    )
    for owner in owners.iterator():
        settings_row, created = TenantNotificationSettings.objects.get_or_create(
            tenant_id=owner.tenant_id,
            defaults={'notify_email': owner.user.email},
        )
        if not created and not settings_row.notify_email:
            settings_row.notify_email = owner.user.email
            settings_row.save(update_fields=['notify_email'])


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0002_telegram_connect_token'),
        ('tenants', '0016_webhook_delivery_claim_constraint'),
        ('users', '0003_user_auth_version'),
    ]

    operations = [
        migrations.RunPython(
            backfill_tenant_notification_settings,
            migrations.RunPython.noop,
        ),
    ]
