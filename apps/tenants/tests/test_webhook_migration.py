"""Regression coverage for live webhook endpoint migration guards."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


@pytest.mark.django_db(transaction=True)
def test_webhook_guard_migration_retires_unsafe_and_duplicate_rows():
    migrate_from = [('tenants', '0013_apikey_least_privilege')]
    migrate_to = [('tenants', '0014_webhook_endpoint_guards')]
    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    old_apps = executor.loader.project_state(migrate_from).apps
    Tenant = old_apps.get_model('tenants', 'Tenant')
    WebhookEndpoint = old_apps.get_model('tenants', 'WebhookEndpoint')
    tenant = Tenant.objects.create(name='Legacy webhooks', slug='legacy-webhooks')
    keeper_id = WebhookEndpoint.objects.create(
        tenant=tenant,
        url='https://hooks.example.com/map',
        secret_encrypted=b'keeper',
        events=['listing.published'],
        is_active=True,
    ).pk
    duplicate_id = WebhookEndpoint.objects.create(
        tenant=tenant,
        url='https://hooks.example.com/map',
        secret_encrypted=b'duplicate',
        events=['listing.published'],
        is_active=False,
    ).pk
    canonical_duplicate_id = WebhookEndpoint.objects.create(
        tenant=tenant,
        url='HTTPS://HOOKS.EXAMPLE.COM:443/map#ignored',
        secret_encrypted=b'canonical-duplicate',
        events=['listing.published'],
        is_active=False,
    ).pk
    unsafe_id = WebhookEndpoint.objects.create(
        tenant=tenant,
        url='http://hooks.example.com/insecure',
        secret_encrypted=b'unsafe',
        events=['listing.published'],
        is_active=True,
    ).pk
    normalized_id = WebhookEndpoint.objects.create(
        tenant=tenant,
        url='HTTPS://hooks.example.com/normalized',
        secret_encrypted=b'normalized',
        events=['listing.published'],
        is_active=True,
    ).pk

    try:
        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        new_apps = executor.loader.project_state(migrate_to).apps
        MigratedEndpoint = new_apps.get_model('tenants', 'WebhookEndpoint')

        keeper = MigratedEndpoint.objects.get(pk=keeper_id)
        duplicate = MigratedEndpoint.objects.get(pk=duplicate_id)
        canonical_duplicate = MigratedEndpoint.objects.get(
            pk=canonical_duplicate_id,
        )
        unsafe = MigratedEndpoint.objects.get(pk=unsafe_id)
        normalized = MigratedEndpoint.objects.get(pk=normalized_id)
        assert keeper.is_active is True
        assert keeper.deleted_at is None
        assert duplicate.is_active is False
        assert duplicate.deleted_at is not None
        assert canonical_duplicate.url == 'https://hooks.example.com/map'
        assert canonical_duplicate.is_active is False
        assert canonical_duplicate.deleted_at is not None
        assert unsafe.is_active is False
        assert unsafe.deleted_at is not None
        assert normalized.url == 'https://hooks.example.com/normalized'
        assert normalized.is_active is True
        assert normalized.deleted_at is None
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_webhook_claim_migration_scrubs_errors_and_claims_inflight_rows():
    migrate_from = [('tenants', '0014_webhook_endpoint_guards')]
    migrate_to = [('tenants', '0016_webhook_delivery_claim_constraint')]
    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    old_apps = executor.loader.project_state(migrate_from).apps
    Tenant = old_apps.get_model('tenants', 'Tenant')
    WebhookEndpoint = old_apps.get_model('tenants', 'WebhookEndpoint')
    WebhookEvent = old_apps.get_model('tenants', 'WebhookEvent')
    WebhookDelivery = old_apps.get_model('tenants', 'WebhookDelivery')

    tenant = Tenant.objects.create(
        name='Legacy delivery errors',
        slug='legacy-delivery-errors',
    )
    endpoints = []
    for suffix in ('retry', 'delivering'):
        endpoints.append(WebhookEndpoint.objects.create(
            tenant=tenant,
            url=f'https://hooks.example.com/{suffix}',
            secret_encrypted=b'legacy-encrypted-secret',
            events=['listing.published'],
            is_active=True,
        ))
    event = WebhookEvent.objects.create(
        tenant=tenant,
        event_type='listing.published',
        payload={'listing_id': 1},
    )
    retry_id = WebhookDelivery.objects.create(
        event=event,
        endpoint=endpoints[0],
        endpoint_url=endpoints[0].url,
        status='retry',
        last_error=(
            'HTTPSConnectionPool(host="hooks.example.com", '
            'url="/retry?token=historic-secret")'
        ),
    ).pk
    last_attempt_at = timezone.now()
    delivering_id = WebhookDelivery.objects.create(
        event=event,
        endpoint=endpoints[1],
        endpoint_url=endpoints[1].url,
        status='delivering',
        last_attempt_at=last_attempt_at,
        last_error='connection failed: api_key=historic-secret',
    ).pk

    try:
        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        new_apps = executor.loader.project_state(migrate_to).apps
        MigratedDelivery = new_apps.get_model('tenants', 'WebhookDelivery')
        retry = MigratedDelivery.objects.get(pk=retry_id)
        delivering = MigratedDelivery.objects.get(pk=delivering_id)

        assert retry.last_error.startswith('legacy_error:')
        assert 'historic-secret' not in retry.last_error
        assert retry.claim_token is None
        assert retry.claimed_at is None
        assert delivering.last_error.startswith('legacy_error:')
        assert 'historic-secret' not in delivering.last_error
        assert delivering.claim_token is not None
        assert delivering.claimed_at == last_attempt_at
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
