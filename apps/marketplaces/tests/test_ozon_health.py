from contextlib import contextmanager
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import connection, transaction
from django.core.management import call_command
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.utils import timezone
from django_celery_beat.models import PeriodicTask

from apps.core.models import BackgroundJobDispatch
from apps.marketplaces import ozon_health_monitor as monitor
from apps.marketplaces.models import MarketplaceAccount, OzonAccountProfile, OzonOfferDraft
from apps.marketplaces.ozon_events import record_automation_result
from apps.marketplaces.ozon_health import health_snapshot, profile_queryset, queue_status, work_snapshot
from apps.marketplaces.serializers import MarketplaceAccountSerializer
from apps.marketplaces.tests.test_ozon_offers import _account, _product, _tenant
from apps.notifications.models import NotificationDelivery, TenantNotificationSettings
from apps.notifications.services import NotificationDeliveryError
from apps.sync.models import SyncLog


@pytest.fixture
def account():
    tenant, _ = _tenant('health-owner')
    account = _account(tenant, 'health-account')
    OzonAccountProfile.objects.filter(account=account).update(orders_auto_sync_enabled=True)
    return account


def load(account):
    return profile_queryset().get(account=account)


def tick(account, now):
    with transaction.atomic():
        monitor.observe_profile(load(account), queue='available', now=now)
    return load(account).health_monitor_state


def error(account, now, *, code='sync_failed', age=20):
    OzonAccountProfile.objects.filter(account=account).update(automation_health={'orders': {
        'checked_at': now.isoformat(), 'failure_since': (now - timedelta(minutes=age)).isoformat(),
        'error_code': code, 'failed': 1,
    }})


def success(account, now):
    OzonAccountProfile.objects.filter(account=account).update(automation_health={'orders': {
        'checked_at': now.isoformat(), 'last_success_at': now.isoformat(), 'error_code': '',
    }})


def notice():
    return BackgroundJobDispatch.objects.filter(task_name=monitor.DELIVERY_TASK).latest('created_at')


def configure(account, enabled=True):
    TenantNotificationSettings.objects.update_or_create(
        tenant=account.tenant, defaults={'telegram_chat_id': '123', 'notify_on_error': enabled},
    )


@pytest.mark.django_db
@pytest.mark.parametrize('enabled,work,age,attempt,state', [
    (False, True, 20, False, 'disabled'), (True, False, 20, False, 'idle'),
    (True, None, 20, False, 'unknown'), (True, True, None, False, 'pending'),
    (True, True, 2, False, 'ok'), (True, True, 20, False, 'delayed'),
    (True, True, 2, True, 'pending'),
])
def test_states_are_local_and_do_not_guess_success(account, enabled, work, age, attempt, state):
    now = timezone.now()
    p = load(account)
    p.orders_auto_sync_enabled = enabled
    if age is not None:
        p.automation_health = {'orders': {'checked_at': (now - timedelta(minutes=age)).isoformat()}}
    if attempt:
        p.orders_checked_at = now
    row = health_snapshot(p, work={'orders': work}, now=now)['jobs'][2]
    assert row['state'] == state
    assert row['alert_ready'] == (state == 'delayed')


@pytest.mark.django_db
def test_new_work_after_idle_has_startup_grace(account):
    now = timezone.now()
    success(account, now - timedelta(days=1))
    tick(account, now)
    assert health_snapshot(load(account), now=now)['jobs'][2]['state'] != 'delayed'
    tick(account, now + timedelta(minutes=16))
    assert notice().args[1] == account.pk


@pytest.mark.django_db
@pytest.mark.parametrize('code,age,ready', [
    ('sync_failed', 1, False), ('sync_failed', 16, True), ('invalid_credentials', 0, True),
    ('PRIVATE TOKEN', 1, False),
])
def test_failure_threshold_and_safe_messages(account, code, age, ready):
    now = timezone.now()
    error(account, now, code=code, age=age)
    snapshot = health_snapshot(load(account), work={'orders': True}, now=now)
    row = snapshot['jobs'][2]
    assert row['state'] == 'error' and row['alert_ready'] == ready
    assert 'PRIVATE TOKEN' not in str(snapshot)
    tick(account, now)
    assert BackgroundJobDispatch.objects.filter(task_name=monitor.DELIVERY_TASK).count() == int(ready)


@pytest.mark.django_db
@pytest.mark.parametrize('days,code', [(None, ''), (10, ''), (6, 'key_expiring'), (-1, 'key_expired')])
def test_credential_expiry_is_not_guessed(account, days, code):
    now = timezone.now()
    p = load(account)
    p.api_key_expires_at = now + timedelta(days=days) if days is not None else None
    assert health_snapshot(p, now=now)['credential']['code'] == code


@pytest.mark.django_db
def test_result_preserves_last_success_and_first_failure(account):
    start = timezone.now()
    for offset, failed in [(0, 0), (1, 1), (2, 1)]:
        with patch('apps.marketplaces.ozon_events.timezone.now', return_value=start + timedelta(minutes=offset)):
            record_automation_result(
                load(account), 'orders', checked=1, failed=failed, code='sync_failed' if failed else '',
            )
    result = load(account).automation_health['orders']
    assert result['last_success_at'] == start.isoformat()
    assert result['failure_since'] == (start + timedelta(minutes=1)).isoformat()
    with patch('apps.marketplaces.ozon_events.timezone.now', return_value=start + timedelta(minutes=3)):
        record_automation_result(load(account), 'orders', checked=1, failed=0, code='')
    assert load(account).automation_health['orders']['failure_since'] is None


@pytest.mark.django_db
def test_one_incident_one_confirmed_recovery_and_cooldown(account):
    now = timezone.now()
    error(account, now)
    state = tick(account, now)
    alert_key = state['incident']['alert_key']
    for minute in (1, 5, 31):
        tick(account, now + timedelta(minutes=minute))
    assert BackgroundJobDispatch.objects.count() == 1
    success(account, now + timedelta(minutes=32))
    tick(account, now + timedelta(minutes=32))
    assert BackgroundJobDispatch.objects.count() == 1
    tick(account, now + timedelta(minutes=34))
    assert BackgroundJobDispatch.objects.count() == 2
    assert notice().args[-1] == alert_key
    tick(account, now + timedelta(minutes=35))
    assert BackgroundJobDispatch.objects.count() == 2
    error(account, now + timedelta(minutes=36))
    tick(account, now + timedelta(minutes=36))
    assert BackgroundJobDispatch.objects.count() == 3


@pytest.mark.django_db
def test_switch_off_closes_quietly_and_recurrence_is_rate_limited(account):
    now = timezone.now()
    error(account, now)
    tick(account, now)
    OzonAccountProfile.objects.filter(account=account).update(orders_auto_sync_enabled=False)
    tick(account, now + timedelta(minutes=1))
    assert load(account).health_monitor_state['incident'] is None
    assert BackgroundJobDispatch.objects.count() == 1
    OzonAccountProfile.objects.filter(account=account).update(orders_auto_sync_enabled=True)
    tick(account, now + timedelta(minutes=2))
    assert BackgroundJobDispatch.objects.count() == 1
    tick(account, now + timedelta(minutes=31))
    assert BackgroundJobDispatch.objects.count() == 2


@pytest.mark.django_db
def test_brief_relapse_resets_recovery_grace(account):
    now = timezone.now()
    error(account, now)
    tick(account, now)
    success(account, now + timedelta(minutes=1))
    tick(account, now + timedelta(minutes=1))
    entry = load(account).automation_health
    entry['orders'].update(error_code='sync_failed', failure_since=(now + timedelta(minutes=2)).isoformat())
    OzonAccountProfile.objects.filter(account=account).update(automation_health=entry)
    tick(account, now + timedelta(minutes=3))
    assert BackgroundJobDispatch.objects.count() == 1
    success(account, now + timedelta(minutes=4))
    tick(account, now + timedelta(minutes=4))
    assert BackgroundJobDispatch.objects.count() == 1
    tick(account, now + timedelta(minutes=6))
    assert BackgroundJobDispatch.objects.count() == 2


@pytest.mark.django_db
def test_completed_reconciliation_is_recovery_even_when_no_more_tasks(account):
    now = timezone.now()
    OzonAccountProfile.objects.filter(account=account).update(automation_health={'reconciliation': {
        'checked_at': now.isoformat(), 'error_code': 'invalid_credentials',
    }})
    with patch.object(monitor, 'work_snapshot', return_value={
        'orders': True, 'commerce': False, 'reconciliation': True,
    }):
        tick(account, now)
    OzonAccountProfile.objects.filter(account=account).update(automation_health={'reconciliation': {
        'checked_at': (now + timedelta(seconds=1)).isoformat(),
        'last_success_at': (now + timedelta(seconds=1)).isoformat(), 'error_code': '',
    }})
    tick(account, now + timedelta(seconds=1))
    tick(account, now + timedelta(minutes=3))
    assert BackgroundJobDispatch.objects.count() == 2
    assert notice().args[-1] is not None


@pytest.mark.django_db
def test_no_recovery_without_successful_job_or_fresh_connection_check(account):
    now = timezone.now()
    OzonAccountProfile.objects.filter(account=account).update(api_key_expires_at=now - timedelta(days=1))
    tick(account, now)
    OzonAccountProfile.objects.filter(account=account).update(api_key_expires_at=now + timedelta(days=30))
    for minute in (1, 4):
        tick(account, now + timedelta(minutes=minute))
    assert BackgroundJobDispatch.objects.count() == 1
    OzonAccountProfile.objects.filter(account=account).update(last_checked_at=now + timedelta(minutes=5))
    tick(account, now + timedelta(minutes=5))
    tick(account, now + timedelta(minutes=7))
    assert BackgroundJobDispatch.objects.count() == 2


@pytest.mark.django_db
def test_outbox_and_incident_rollback_atomically(account):
    error(account, timezone.now())
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            tick(account, timezone.now())
            raise RuntimeError('rollback')
    assert not load(account).health_monitor_state
    assert not BackgroundJobDispatch.objects.exists()
    assert not SyncLog.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_notice_survives_unavailable_broker(account, django_capture_on_commit_callbacks):
    error(account, timezone.now())
    with patch('apps.core.dispatch.current_app.send_task', side_effect=ConnectionError('broker unavailable')):
        with django_capture_on_commit_callbacks(execute=True):
            tick(account, timezone.now())
    assert notice().status == BackgroundJobDispatch.Status.PENDING
    assert load(account).health_monitor_state['incident']['alert_key'] == notice().deduplication_key


@pytest.mark.django_db
def test_delivery_fenced_by_tenant_account_and_current_incident(account):
    now = timezone.now()
    error(account, now)
    tick(account, now)
    args = notice().args
    other, _ = _tenant('health-other')
    second = _account(account.tenant, 'health-second')
    configure(account)
    with patch.object(monitor.NotificationService, 'notify') as send:
        assert monitor.deliver_alert(other.pk, *args[1:])['status'] == 'superseded'
        assert monitor.deliver_alert(args[0], second.pk, *args[2:])['status'] == 'superseded'
        assert monitor.deliver_alert(args[0], args[1], 'wrong-key', *args[3:])['status'] == 'superseded'
        send.assert_not_called()
        assert monitor.deliver_alert(*args)['status'] == 'delivered'
        send.assert_called_once()
        OzonAccountProfile.objects.filter(account=account).update(orders_auto_sync_enabled=False)
        assert monitor.deliver_alert(*args)['status'] == 'superseded'
        assert send.call_count == 1


@pytest.mark.django_db
def test_both_notice_types_respect_mute_and_recovery_requires_delivered_alert(account):
    now = timezone.now()
    error(account, now)
    tick(account, now)
    alert = notice()
    configure(account, enabled=False)
    assert monitor.deliver_alert(*alert.args)['status'] == 'muted_or_unconfigured'
    success(account, now + timedelta(seconds=1))
    tick(account, now + timedelta(seconds=1))
    tick(account, now + timedelta(minutes=3))
    recovery = notice()
    assert monitor.deliver_alert(*recovery.args)['status'] == 'muted_or_unconfigured'
    configure(account)
    assert monitor.deliver_alert(*recovery.args)['status'] == 'no_delivered_incident'
    NotificationDelivery.objects.create(
        tenant=account.tenant, event_key=alert.args[2], channel='telegram',
        status='sent', payload_fingerprint='a' * 64,
    )
    with patch.object(monitor.NotificationService, 'notify') as send:
        assert monitor.deliver_alert(*recovery.args)['status'] == 'delivered'
        assert send.call_args.args[1] == 'success'
        error(account, timezone.now(), code='invalid_credentials')
        assert monitor.deliver_alert(*recovery.args)['status'] == 'superseded'
        assert send.call_count == 1


@pytest.mark.django_db
@pytest.mark.parametrize('retryable', [True, False])
def test_delivery_retries_only_known_safe_failures(account, retryable):
    error(account, timezone.now())
    tick(account, timezone.now())
    configure(account)
    failure = NotificationDeliveryError('private provider details', retryable=retryable)
    with patch.object(monitor.NotificationService, 'notify', side_effect=failure):
        if retryable:
            with pytest.raises(RuntimeError, match='safe delivery retry'):
                monitor.deliver_alert(*notice().args)
        else:
            assert monitor.deliver_alert(*notice().args)['status'] == 'delivery_needs_review'


@pytest.mark.django_db
def test_notification_escapes_name_and_reuses_immutable_message(account):
    account.name = '<b>Shop & Co</b>'
    account.save(update_fields=['name'])
    error(account, timezone.now())
    tick(account, timezone.now())
    assert '&lt;b&gt;Shop &amp; Co&lt;/b&gt;' in notice().args[3]
    assert '<b>Shop' not in notice().args[3]


@pytest.mark.django_db
def test_monitor_is_bounded_fair_and_excludes_disabled_tenants(account):
    second = _account(account.tenant, 'health-second')
    third = _account(account.tenant, 'health-third')
    other, _ = _tenant('health-inactive')
    foreign = _account(other, 'health-inactive')
    other.is_active = False
    other.save(update_fields=['is_active'])
    with (
        patch.object(monitor, 'BATCH_SIZE', 2),
        patch.object(monitor, 'get_cached_celery_queue_snapshot', return_value=None),
    ):
        assert monitor.monitor_accounts() == {'checked': 2, 'busy': False}
        assert monitor.monitor_accounts() == {'checked': 1, 'busy': False}
        assert monitor.monitor_accounts() == {'checked': 0, 'busy': False}
    assert all(load(a).health_monitor_checked_at for a in (account, second, third))
    assert load(foreign).health_monitor_checked_at is None


@pytest.mark.django_db
def test_busy_monitor_and_deadline_do_not_touch_profiles(account):
    @contextmanager
    def busy(*args):
        yield False

    with patch.object(monitor, 'try_session_advisory_lock', busy):
        assert monitor.monitor_accounts() == {'checked': 0, 'busy': True}
    with patch.object(monitor, 'time', SimpleNamespace(monotonic=iter([0, 20]).__next__)):
        with patch.object(monitor, 'get_cached_celery_queue_snapshot', return_value=None):
            assert monitor.monitor_accounts()['checked'] == 0
    assert load(account).health_monitor_checked_at is None


@pytest.mark.django_db
def test_read_only_endpoint_is_tenant_scoped_and_has_no_provider_calls(account):
    tenant, key = _tenant('health-api')
    own = _account(tenant, 'health-api')
    avito = _account(tenant, 'health-avito', marketplace='avito')
    client = Client(HTTP_AUTHORIZATION=f'Bearer {key}')
    original = load(own).updated_at
    with patch('apps.marketplaces.ozon_health_views.get_cached_celery_queue_snapshot', return_value=None):
        with patch(
            'apps.marketplaces.adapters.ozon.client.OzonSellerClient._post',
            side_effect=AssertionError('provider I/O'),
        ):
            response = client.get(f'/api/v1/accounts/{own.pk}/ozon-health/')
            assert response.status_code == 200
            assert response.json()['data']['jobs'][1]['state'] == 'disabled'
            assert client.get(f'/api/v1/accounts/{account.pk}/ozon-health/').status_code == 404
            assert client.get(f'/api/v1/accounts/{avito.pk}/ozon-health/').status_code == 404
            # Machine keys have no POST scope here: permission fails before method dispatch.
            assert client.post(f'/api/v1/accounts/{own.pk}/ozon-health/').status_code == 403
    assert load(own).health_monitor_state == {} and load(own).updated_at == original
    assert not BackgroundJobDispatch.objects.exists()


@pytest.mark.django_db
def test_account_serializer_health_has_no_extra_queries(account, django_assert_num_queries):
    account = MarketplaceAccount.objects.select_related('ozon_profile').get(pk=account.pk)
    with django_assert_num_queries(0):
        snapshot = MarketplaceAccountSerializer().get_ozon_profile(account)
    assert snapshot['sync_health']['jobs'][1]['state'] == 'disabled'
    assert 'secret' not in str(snapshot)


@pytest.mark.django_db
def test_work_detection_rejects_foreign_products(account):
    other, _ = _tenant('health-foreign-product')
    OzonOfferDraft.objects.create(
        tenant=account.tenant, account=account, product=_product(other),
        publication_status='published', provider_product_id=123,
    )
    assert work_snapshot(load(account))['commerce'] is False


@pytest.mark.django_db
def test_monitor_registration_uses_existing_queue_and_short_expiry():
    call_command('setup_periodic_tasks', stdout=StringIO())
    task = PeriodicTask.objects.get(name='monitor_ozon_account_health')
    assert task.task == 'apps.marketplaces.ozon_tasks.monitor_ozon_account_health'
    assert task.queue == 'notifications' and task.expire_seconds == 50
    assert task.interval.every == 1 and task.interval.period == 'minutes'


@pytest.mark.django_db
def test_successful_delivery_is_idempotent_through_shared_notification_service(account):
    error(account, timezone.now())
    tick(account, timezone.now())
    configure(account)
    with patch('apps.notifications.services.TelegramNotifier.send', return_value=True) as send:
        assert monitor.deliver_alert(*notice().args)['status'] == 'delivered'
        assert monitor.deliver_alert(*notice().args)['status'] == 'delivered'
    send.assert_called_once()
    assert NotificationDelivery.objects.get(event_key=notice().args[2]).status == 'sent'


@pytest.mark.parametrize('snapshot,expected', [
    (None, 'unknown'), ({}, 'unknown'),
    ({'broker_status': 'ok', 'worker_status': 'ok', 'queues': {'sync_import': {'subscribed_workers': 1}}}, 'available'),
    ({'broker_status': 'ok', 'worker_status': 'ok', 'queues': {
        'sync_import': {'subscribed_workers': 0},
    }}, 'unavailable'),
])
def test_queue_evidence_is_not_invented(snapshot, expected):
    assert queue_status(snapshot) == expected


@pytest.mark.django_db(transaction=True)
def test_upgrade_preserves_automation_and_account_data(account):
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([('marketplaces', '0046_ozon_automation_cursors')])
        old_apps = executor.loader.project_state([('marketplaces', '0046_ozon_automation_cursors')]).apps
        old_apps.get_model('marketplaces', 'OzonAccountProfile').objects.filter(account_id=account.pk).update(
            automation_health={'orders': {'error_code': 'sync_failed'}}, commerce_cursor=42,
        )
        MigrationExecutor(connection).migrate(latest)
        p = load(account)
        assert p.automation_health == {'orders': {'error_code': 'sync_failed'}}
        assert p.commerce_cursor == 42 and p.orders_auto_sync_enabled
        assert p.health_monitor_state == {} and p.health_monitor_checked_at is None
    finally:
        MigrationExecutor(connection).migrate(latest)
