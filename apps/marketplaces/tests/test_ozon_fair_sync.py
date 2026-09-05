from contextlib import contextmanager
from datetime import timedelta
import threading
from unittest.mock import patch
import uuid

import pytest
from django.db import connection, close_old_connections
from django.utils import timezone

from apps.core.advisory_lock import try_session_advisory_lock
from apps.marketplaces.models import OzonAccountProfile, OzonOfferDraft, OzonOperation
from apps.marketplaces import ozon_tasks
from apps.marketplaces.ozon_orders import OzonOrderSyncError
from apps.marketplaces.tests.test_ozon_offers import _account, _product, _tenant
from apps.sync.models import SyncLog


def _enabled_account(tenant, name):
    account = _account(tenant, name)
    OzonAccountProfile.objects.filter(account=account).update(
        commerce_auto_sync_enabled=True, product_write_enabled=True, orders_auto_sync_enabled=True,
    )
    return account


def _offers(tenant, account, count):
    return [OzonOfferDraft.objects.create(
        tenant=tenant, account=account, product=_product(tenant, f'{account.pk}-{i}'),
        publication_status='published', provider_product_id=i + 1,
    ) for i in range(count)]


def _due_again():
    old = timezone.now() - timedelta(minutes=10)
    OzonAccountProfile.objects.update(
        commerce_checked_at=old, orders_checked_at=old, reconciliation_checked_at=old,
    )


@pytest.mark.django_db
def test_commerce_passes_100_cards_and_wraps_without_duplicates(settings):
    settings.DEBUG = True  # SQLite fallback; concurrency has its own PostgreSQL test.
    tenant, _ = _tenant('fair-many-cards')
    account = _enabled_account(tenant, 'fair-many-cards')
    drafts = _offers(tenant, account, 103)
    seen = []
    with patch.object(ozon_tasks, 'sync_offer_commerce', side_effect=lambda p, a, **kw: seen.append(p.pk)):
        assert ozon_tasks.sync_enabled_ozon_commerce()['attempted'] == 100
        assert seen == [d.product_id for d in drafts[:100]]
        _due_again()
        seen.clear()
        assert ozon_tasks.sync_enabled_ozon_commerce()['attempted'] == 100
    assert seen[:3] == [d.product_id for d in drafts[100:]]
    assert len(set(seen)) == 100


@pytest.mark.django_db
def test_orders_pass_20_accounts_across_tenants_even_when_first_account_fails(settings):
    settings.DEBUG = True
    tenant, _ = _tenant('fair-many-accounts')
    other, _ = _tenant('fair-other-tenant')
    accounts = [_enabled_account(tenant if i < 20 else other, f'fair-orders-{i}') for i in range(21)]
    seen = []

    def sync(account):
        seen.append(account.pk)
        if account.pk == accounts[0].pk:
            raise OzonOrderSyncError('invalid_credentials', 'secret-must-not-be-logged')
        return 0

    with patch.object(ozon_tasks, 'sync_fbs_orders', side_effect=sync):
        first = ozon_tasks.sync_enabled_ozon_orders()
        assert first['attempted'] == 20 and first['failed'] == 1
        second = ozon_tasks.sync_enabled_ozon_orders()
        assert second['attempted'] == 1
    assert seen == [a.pk for a in accounts]
    log = SyncLog.objects.get(account=accounts[0])
    assert 'secret-must-not-be-logged' not in str(log.payload) + log.message
    assert log.payload['error_code'] == 'invalid_credentials'


@pytest.mark.django_db
def test_large_account_does_not_consume_small_accounts_budget(settings):
    settings.DEBUG = True
    tenant, _ = _tenant('fair-small-account')
    large = _enabled_account(tenant, 'fair-large')
    small = _enabled_account(tenant, 'fair-small')
    _offers(tenant, large, 102)
    small_offer = _offers(tenant, small, 1)[0]
    with patch.object(ozon_tasks, 'sync_offer_commerce') as sync:
        result = ozon_tasks.sync_enabled_ozon_commerce()
    assert result['accounts'] == 2
    assert result['attempted'] == 51
    assert sync.call_args.args[0].pk == small_offer.product_id


@pytest.mark.django_db
def test_busy_account_does_not_block_other_accounts_or_advance_its_cursor(settings):
    settings.DEBUG = True
    tenant, _ = _tenant('fair-busy')
    busy = _enabled_account(tenant, 'fair-busy-1')
    free = _enabled_account(tenant, 'fair-busy-2')

    @contextmanager
    def lock(identity):
        yield identity != f'ozon:automation:account:{busy.ozon_profile.pk}'

    with patch.object(ozon_tasks, 'try_session_advisory_lock', side_effect=lock):
        with patch.object(ozon_tasks, 'sync_fbs_orders') as sync:
            result = ozon_tasks.sync_enabled_ozon_orders()
    assert result['busy'] == 1 and result['accounts'] == 1
    sync.assert_called_once_with(free)
    busy.ozon_profile.refresh_from_db()
    assert busy.ozon_profile.orders_checked_at is None


@pytest.mark.django_db
def test_switch_disabled_after_selection_prevents_provider_call(settings):
    settings.DEBUG = True
    tenant, _ = _tenant('fair-switch')
    account = _enabled_account(tenant, 'fair-switch')

    @contextmanager
    def lock(identity):
        OzonAccountProfile.objects.filter(account=account).update(orders_auto_sync_enabled=False)
        yield True

    with patch.object(ozon_tasks, 'try_session_advisory_lock', side_effect=lock):
        with patch.object(ozon_tasks, 'sync_fbs_orders') as sync:
            assert ozon_tasks.sync_enabled_ozon_orders()['attempted'] == 0
    sync.assert_not_called()


@pytest.mark.django_db
def test_failure_and_recovery_logged_once_and_never_expose_exception(settings):
    settings.DEBUG = True
    tenant, _ = _tenant('fair-recovery')
    account = _enabled_account(tenant, 'fair-recovery')
    with patch.object(ozon_tasks, 'sync_fbs_orders', side_effect=RuntimeError('PRIVATE TOKEN')):
        for _ in range(2):
            assert ozon_tasks.sync_enabled_ozon_orders()['failed'] == 1
            _due_again()
    assert SyncLog.objects.filter(account=account).count() == 1
    with patch.object(ozon_tasks, 'sync_fbs_orders', return_value=0):
        for _ in range(2):
            assert ozon_tasks.sync_enabled_ozon_orders()['checked'] == 1
            _due_again()
    logs = SyncLog.objects.filter(account=account).order_by('pk')
    assert [log.status for log in logs] == ['warn', 'ok']
    assert all('PRIVATE TOKEN' not in log.message + str(log.payload) for log in logs)


@pytest.mark.django_db
def test_failed_commerce_result_is_not_reported_as_healthy(settings):
    settings.DEBUG = True
    tenant, _ = _tenant('fair-failed-result')
    account = _enabled_account(tenant, 'fair-failed-result')
    _offers(tenant, account, 1)
    outcome = OzonOperation(state='outcome_unknown')
    with patch.object(ozon_tasks, 'sync_offer_commerce', return_value=(outcome, None)):
        assert ozon_tasks.sync_enabled_ozon_commerce()['failed'] == 1
    account.ozon_profile.refresh_from_db()
    assert account.ozon_profile.automation_health['commerce']['failed'] == 1


@pytest.mark.django_db
def test_disable_commerce_during_batch_stops_remaining_writes(settings):
    settings.DEBUG = True
    tenant, _ = _tenant('fair-switch-mid-batch')
    account = _enabled_account(tenant, 'fair-switch-mid-batch')
    _offers(tenant, account, 3)

    def disable(product, account, **kwargs):
        OzonAccountProfile.objects.filter(account=account).update(commerce_auto_sync_enabled=False)

    with patch.object(ozon_tasks, 'sync_offer_commerce', side_effect=disable) as sync:
        assert ozon_tasks.sync_enabled_ozon_commerce()['attempted'] == 1
    assert sync.call_count == 1


@pytest.mark.django_db
def test_scheduler_rejects_inconsistent_foreign_product_relations(settings):
    settings.DEBUG = True
    tenant, _ = _tenant('fair-bad-relation')
    other, _ = _tenant('fair-bad-relation-other')
    account = _enabled_account(tenant, 'fair-bad-relation')
    OzonOfferDraft.objects.create(
        tenant=tenant, account=account, product=_product(other),
        publication_status='published', provider_product_id=12,
    )
    with patch.object(ozon_tasks, 'sync_offer_commerce') as sync:
        assert ozon_tasks.sync_enabled_ozon_commerce()['attempted'] == 0
    sync.assert_not_called()


@pytest.mark.django_db
def test_run_deadline_leaves_next_cards_for_a_later_run(settings):
    settings.DEBUG = True
    tenant, _ = _tenant('fair-deadline')
    account = _enabled_account(tenant, 'fair-deadline')
    drafts = _offers(tenant, account, 3)
    with patch.object(ozon_tasks.time, 'monotonic', side_effect=[0, 0, 0, 50]):
        with patch.object(ozon_tasks, 'sync_offer_commerce') as sync:
            assert ozon_tasks.sync_enabled_ozon_commerce()['attempted'] == 1
    sync.assert_called_once()
    account.ozon_profile.refresh_from_db()
    assert account.ozon_profile.commerce_cursor == drafts[0].pk


@pytest.mark.django_db
def test_reconciliation_cursor_advances_past_failed_operations(settings):
    settings.DEBUG = True
    tenant, _ = _tenant('fair-reconcile')
    account = _enabled_account(tenant, 'fair-reconcile')
    drafts = _offers(tenant, account, 3)
    for draft in drafts:
        OzonOperation.objects.create(
            tenant=tenant, account=account, offer=draft, kind='product_import',
            state='reconciling', idempotency_key=str(uuid.uuid4()), request_sha256='a' * 64,
        )
    seen = []

    def fail(product, account):
        seen.append(product.pk)
        raise ozon_tasks.OzonReconciliationError('account_not_ready', 'Cannot read')

    with patch.object(ozon_tasks, 'ITEM_BATCH_SIZE', 2):
        with patch.object(ozon_tasks, 'reconcile_product_import', side_effect=fail):
            assert ozon_tasks.reconcile_due_ozon_imports()['attempted'] == 2
            _due_again()
            assert ozon_tasks.reconcile_due_ozon_imports()['attempted'] == 2
    assert set(seen) == {draft.product_id for draft in drafts}


@pytest.mark.django_db(transaction=True)
def test_concurrent_account_jobs_do_not_overlap_and_lock_releases():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires PostgreSQL session locks')
    tenant, _ = _tenant('fair-concurrency')
    account = _enabled_account(tenant, 'fair-concurrency')
    _offers(tenant, account, 1)
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def hold(account):
        entered.set()
        assert release.wait(10)
        return 0

    def worker():
        close_old_connections()
        try:
            ozon_tasks.sync_enabled_ozon_orders()
        except Exception as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    with patch.object(ozon_tasks, 'sync_fbs_orders', side_effect=hold):
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        try:
            assert entered.wait(10)
            with patch.object(ozon_tasks, 'sync_offer_commerce') as sync:
                assert ozon_tasks.sync_enabled_ozon_commerce()['busy'] == 1
                sync.assert_not_called()
        finally:
            release.set()
            thread.join(10)
    assert not thread.is_alive() and not errors
    with try_session_advisory_lock(f'ozon:automation:account:{account.ozon_profile.pk}') as acquired:
        assert acquired
