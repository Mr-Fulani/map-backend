import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_notification_settings_migration_backfills_owner_email_without_overwrite():
    migrate_from = [
        ('notifications', '0002_telegram_connect_token'),
        ('tenants', '0016_webhook_delivery_claim_constraint'),
        ('users', '0003_user_auth_version'),
    ]
    migrate_to = [('notifications', '0003_backfill_tenant_notification_settings')]
    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    old_apps = executor.loader.project_state(migrate_from).apps
    User = old_apps.get_model('users', 'User')
    Tenant = old_apps.get_model('tenants', 'Tenant')
    TenantUser = old_apps.get_model('tenants', 'TenantUser')
    NotificationSettings = old_apps.get_model(
        'notifications',
        'TenantNotificationSettings',
    )

    cases = []
    for suffix, existing_email in (
        ('missing', None),
        ('blank', ''),
        ('custom', 'alerts@example.com'),
    ):
        user = User.objects.create(email=f'owner-{suffix}@example.com')
        tenant = Tenant.objects.create(
            name=f'Legacy {suffix}',
            slug=f'legacy-notifications-{suffix}',
        )
        TenantUser.objects.create(user=user, tenant=tenant, role='owner')
        if existing_email is not None:
            NotificationSettings.objects.create(
                tenant=tenant,
                notify_email=existing_email,
            )
        cases.append((tenant.pk, user.email, existing_email))

    try:
        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        new_apps = executor.loader.project_state(migrate_to).apps
        MigratedSettings = new_apps.get_model(
            'notifications',
            'TenantNotificationSettings',
        )

        for tenant_id, owner_email, existing_email in cases:
            settings_row = MigratedSettings.objects.get(tenant_id=tenant_id)
            assert settings_row.notify_email == (existing_email or owner_email)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_notification_delivery_migration_applies_on_existing_database():
    migrate_from = [('notifications', '0003_backfill_tenant_notification_settings')]
    migrate_to = [('notifications', '0004_notificationdelivery')]
    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    try:
        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        new_apps = executor.loader.project_state(migrate_to).apps
        Delivery = new_apps.get_model('notifications', 'NotificationDelivery')
        assert Delivery.objects.count() == 0
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
