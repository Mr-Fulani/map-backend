import uuid

import pytest
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client

from apps.marketplaces.models import Listing, OzonOfferDraft, OzonOperation
from apps.marketplaces.tests.test_ozon_offers import _account, _product, _tenant
from apps.sync.models import SyncLog


def _operation(tenant, account, product, **kwargs):
    draft = OzonOfferDraft.objects.create(tenant=tenant, account=account, product=product)
    return OzonOperation.objects.create(
        tenant=tenant, account=account, offer=draft,
        kind=kwargs.pop('kind', 'product_import'), state=kwargs.pop('state', 'queued'),
        idempotency_key=str(uuid.uuid4()), request_sha256='a' * 64, **kwargs,
    )


@pytest.mark.django_db
def test_operation_events_track_lifecycle_without_duplicate_polling_or_secrets():
    tenant, _ = _tenant('ozon-event-lifecycle')
    account = _account(tenant, 'ozon-event-lifecycle')
    product = _product(tenant)
    operation = _operation(tenant, account, product, request_summary={'api_key': 'PRIVATE'})
    operation.state = 'reconciling'
    for _ in range(3):
        operation.save(update_fields=['state'])
    operation.state = 'succeeded'
    operation.response_summary = {'provider_warnings': [{'message': 'PRIVATE'}]}
    operation.save(update_fields=['state', 'response_summary'])
    operation.save(update_fields=['state', 'response_summary'])
    logs = list(SyncLog.objects.filter(account=account).order_by('pk'))
    assert len(logs) == 3
    assert [log.payload['state'] for log in logs] == ['queued', 'reconciling', 'succeeded']
    assert [log.status for log in logs] == ['ok', 'ok', 'warn']
    assert all(log.product_id == product.pk and log.listing_id is None for log in logs)
    assert all('PRIVATE' not in log.message + str(log.payload) for log in logs)
    assert Listing.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize('kind,event_type', [
    ('price_update', 'listing_price_update'), ('stock_update', 'listing_stock_update'),
    ('archive', 'listing_unpublish'), ('product_import', 'listing_publish'),
])
def test_operation_results_have_matching_log_filter(kind, event_type):
    tenant, _ = _tenant(f'ozon-event-{kind}')
    account = _account(tenant, f'ozon-event-{kind}')
    _operation(tenant, account, _product(tenant), kind=kind, state='failed', errors=[{'message': 'PRIVATE'}])
    log = SyncLog.objects.get(account=account)
    assert log.event_type == event_type and log.status == 'error'
    assert 'PRIVATE' not in log.message + str(log.payload)


@pytest.mark.django_db
def test_operation_and_event_roll_back_together():
    tenant, _ = _tenant('ozon-event-rollback')
    account = _account(tenant, 'ozon-event-rollback')
    product = _product(tenant)
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            _operation(tenant, account, product)
            raise RuntimeError('rollback')
    assert not OzonOperation.objects.exists()
    assert not SyncLog.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_logs_filter_exact_ozon_account_and_link_without_an_avito_listing():
    tenant, key = _tenant('ozon-event-owner')
    other, other_key = _tenant('ozon-event-other')
    account = _account(tenant, 'ozon-event-owner')
    second = _account(tenant, 'ozon-event-second')
    foreign = _account(other, 'ozon-event-other')
    product = _product(tenant)
    _operation(tenant, account, product)
    _operation(tenant, second, product)
    _operation(other, foreign, _product(other))
    client = Client()
    response = client.get(
        '/api/v1/logs/', {'marketplace': 'ozon', 'account': account.pk},
        HTTP_AUTHORIZATION=f'Bearer {key}',
    )
    assert response.status_code == 200
    assert response.json()['meta']['total'] == 1
    item = response.json()['data'][0]
    assert item['account_name'] == account.name
    assert item['target_url'] == f'/dashboard/listings?product={product.pk}&target={account.pk}'
    assert item['listing'] is None and item['marketplace'] == 'ozon'
    denied = client.get(
        '/api/v1/logs/', {'marketplace': 'ozon', 'account': account.pk},
        HTTP_AUTHORIZATION=f'Bearer {other_key}',
    )
    assert denied.json()['meta']['total'] == 0


@pytest.mark.django_db
def test_inconsistent_operation_cannot_project_a_foreign_product_to_logs():
    tenant, _ = _tenant('ozon-event-fence')
    other, _ = _tenant('ozon-event-fence-other')
    account = _account(tenant, 'ozon-event-fence')
    _operation(tenant, account, _product(other))
    assert not SyncLog.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_explicit_foreign_log_account_is_hidden_and_not_used_for_a_link():
    tenant, key = _tenant('ozon-event-bad-link')
    other, _ = _tenant('ozon-event-bad-link-other')
    foreign = _account(other, 'ozon-event-bad-link-other')
    SyncLog.objects.create(
        tenant=tenant, account=foreign, event_type='orders_sync', status='warn', message='bad relation',
    )
    client = Client()
    response = client.get('/api/v1/logs/', HTTP_AUTHORIZATION=f'Bearer {key}')
    row = response.json()['data'][0]
    assert row['account_id'] is None and row['target_url'] is None
    filtered = client.get('/api/v1/logs/', {'marketplace': 'ozon'}, HTTP_AUTHORIZATION=f'Bearer {key}')
    assert filtered.json()['meta']['total'] == 0


@pytest.mark.django_db(transaction=True)
def test_upgrade_preserves_existing_accounts_and_legacy_logs():
    tenant, _ = _tenant('ozon-event-upgrade')
    account = _account(tenant, 'ozon-event-upgrade')
    log = SyncLog.objects.create(
        tenant=tenant, event_type='listing_publish', status='ok', message='Existing Avito event',
    )
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([
            ('marketplaces', '0045_ozon_provider_barcodes'),
            ('sync', '0002_alter_synclog_options_alter_synclog_created_at_and_more'),
        ])
        MigrationExecutor(connection).migrate(latest)
        account.ozon_profile.refresh_from_db()
        assert account.ozon_profile.commerce_cursor == 0
        assert account.ozon_profile.orders_checked_at is None
        assert account.ozon_profile.automation_health == {}
        log.refresh_from_db()
        assert log.message == 'Existing Avito event'
        assert log.account_id is None and log.event_key is None
    finally:
        MigrationExecutor(connection).migrate(latest)
