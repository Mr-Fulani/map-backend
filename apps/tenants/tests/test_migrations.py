"""Data-migration regressions for tenant security boundaries."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


@pytest.mark.django_db(transaction=True)
def test_api_key_migration_revokes_legacy_and_disambiguates_duplicates():
    migrate_from = [('tenants', '0012_webhook_outbox_and_encrypted_secret')]
    migrate_to = [('tenants', '0013_apikey_least_privilege')]
    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    old_apps = executor.loader.project_state(migrate_from).apps
    Tenant = old_apps.get_model('tenants', 'Tenant')
    APIKey = old_apps.get_model('tenants', 'APIKey')
    tenant = Tenant.objects.create(name='Legacy Keys', slug='legacy-keys')
    duplicate_hash = 'd' * 64
    duplicate_ids = [
        APIKey.objects.create(
            tenant=tenant,
            name=f'Duplicate {index}',
            key_prefix=f'legacy{index}',
            key_hash=duplicate_hash,
            is_active=True,
        ).pk
        for index in range(2)
    ]
    active_id = APIKey.objects.create(
        tenant=tenant,
        name='Legacy active',
        key_prefix='legacyact',
        key_hash='a' * 64,
        is_active=True,
    ).pk
    inactive_id = APIKey.objects.create(
        tenant=tenant,
        name='Legacy inactive',
        key_prefix='legacyoff',
        key_hash='b' * 64,
        is_active=False,
    ).pk

    try:
        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        new_apps = executor.loader.project_state(migrate_to).apps
        MigratedAPIKey = new_apps.get_model('tenants', 'APIKey')
        migrated = {
            key.pk: key
            for key in MigratedAPIKey.objects.filter(tenant_id=tenant.pk)
        }

        assert set(migrated) == {*duplicate_ids, active_id, inactive_id}
        assert all(not key.is_active for key in migrated.values())
        assert all(key.revoked_at is not None for key in migrated.values())
        assert migrated[active_id].role == 'viewer'
        assert migrated[active_id].scopes == ['tenant:read']
        assert migrated[active_id].expires_at > timezone.now()
        duplicate_keys = [migrated[key_id] for key_id in duplicate_ids]
        assert len({key.key_hash for key in duplicate_keys}) == 2
        assert duplicate_hash not in {key.key_hash for key in duplicate_keys}
        assert MigratedAPIKey.objects.filter(
            tenant_id=tenant.pk,
        ).values('key_hash').distinct().count() == 4
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
