"""Local-only Ozon health projection: no provider I/O and no read-side writes."""

from datetime import timedelta

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.marketplaces.models import OzonAccountProfile, OzonOfferDraft, OzonOperation
from apps.marketplaces.ozon_events import ERROR_MESSAGES


STALE_AFTER = timedelta(minutes=15)
MONITOR_STALE_AFTER = timedelta(minutes=5)
JOB_LABELS = {
    'reconciliation': 'Проверка статусов карточек',
    'commerce': 'Цены и остатки',
    'orders': 'Заказы FBS',
}
AUTH_ERRORS = {'auth_error', 'invalid_credentials', 'expired_credentials'}


def as_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = parse_datetime(value)
    except (ValueError, TypeError):
        return None
    return parsed if parsed is not None and timezone.is_aware(parsed) else None


def profile_queryset():
    """Two indexed existence probes, independent of catalog size."""
    own = dict(account_id=OuterRef('account_id'), tenant_id=OuterRef('account__tenant_id'))
    commerce = OzonOfferDraft.objects.filter(
        **own, product__tenant_id=OuterRef('account__tenant_id'),
        publication_status='published', provider_product_id__isnull=False,
    )
    due = OzonOperation.objects.filter(
        **own, offer__account_id=OuterRef('account_id'),
        offer__tenant_id=OuterRef('account__tenant_id'),
        offer__product__tenant_id=OuterRef('account__tenant_id'),
        kind__in=('product_import', 'archive'), state__in=OzonOperation.ACTIVE_STATES,
    ).filter(Q(next_reconcile_at__isnull=True) | Q(next_reconcile_at__lte=timezone.now()))
    return OzonAccountProfile.objects.filter(account__marketplace='ozon').select_related(
        'account', 'account__tenant',
    ).annotate(health_has_commerce=Exists(commerce), health_has_reconciliation=Exists(due))


def work_snapshot(profile):
    return {
        'commerce': profile.health_has_commerce,
        'reconciliation': profile.health_has_reconciliation,
        'orders': True,
    }


def queue_status(snapshot):
    if not isinstance(snapshot, dict):
        return 'unknown'
    if snapshot.get('broker_status') != 'ok' or snapshot.get('worker_status') != 'ok':
        return 'unknown'
    queue = snapshot.get('queues', {}).get('sync_import', {})
    workers = queue.get('subscribed_workers')
    if type(workers) is int:
        return 'available' if workers > 0 else 'unavailable'
    return 'unknown'


def health_snapshot(profile, *, work=None, queue=None, now=None):
    now = now or timezone.now()
    monitor = profile.health_monitor_state if isinstance(profile.health_monitor_state, dict) else {}
    health = profile.automation_health if isinstance(profile.automation_health, dict) else {}
    work = work if work is not None else monitor.get('work', {})
    work = work if isinstance(work, dict) else {}
    active = profile.account.is_active
    enabled = {
        'reconciliation': active,
        'commerce': active and profile.product_write_enabled and profile.commerce_auto_sync_enabled,
        'orders': active and profile.orders_auto_sync_enabled,
    }
    jobs = []
    for job, label in JOB_LABELS.items():
        entry = health.get(job, {})
        entry = entry if isinstance(entry, dict) else {}
        completed = as_time(entry.get('checked_at'))
        success = as_time(entry.get('last_success_at'))
        if success is None and not entry.get('error_code'):
            success = completed  # compatibility with pre-monitor snapshots
        attempted = getattr(profile, f'{job}_checked_at')
        code = entry.get('error_code')
        code = code if code in ERROR_MESSAGES or code in AUTH_ERRORS else ('sync_failed' if code else '')
        watches = monitor.get('watch_since', {})
        watch = as_time(watches.get(job)) if isinstance(watches, dict) else None
        # A new workload after a long idle period gets its own startup grace.
        baseline = max(value for value in (completed, watch) if value is not None) if completed or watch else now
        state, message, alert = 'pending', 'Ожидаем первый успешный запуск.', False
        if not enabled[job]:
            state, message = 'disabled', 'Автоматизация выключена. Это не ошибка.'
        elif work.get(job) is False:
            state, message = 'idle', 'Сейчас нет карточек, ожидающих фоновой проверки.'
        elif work.get(job) is not True:
            state, message = 'unknown', 'Монитор ещё не подтвердил наличие задач.'
        elif code:
            state = 'error'
            message = ERROR_MESSAGES.get(code, 'Синхронизация не завершена. Проверьте кабинет и журнал.')
            since = as_time(entry.get('failure_since')) or completed or now
            alert = code in AUTH_ERRORS or now - since >= STALE_AFTER
        elif now - baseline >= STALE_AFTER:
            state, code, alert = 'delayed', 'sync_delayed', True
            message = 'Более 15 минут нет завершённого запуска при наличии задач.'
        elif attempted and (completed is None or attempted > completed):
            message = 'Запуск начат; результат ещё не подтверждён.'
        elif completed:
            state, message = 'ok', 'Последний запуск завершился успешно.'
        jobs.append({
            'job': job, 'label': label, 'state': state, 'message': message,
            'last_attempt_at': attempted, 'last_completed_at': completed,
            'last_success_at': success, 'error_code': code if state == 'error' else '',
            'alert_ready': alert,
        })
    key = {'state': 'ok', 'message': 'Срок ключа не требует действия.', 'code': ''}
    expiry = profile.api_key_expires_at
    if not active:
        key = {'state': 'disabled', 'message': 'Кабинет выключен.', 'code': ''}
    elif profile.connection_status != 'connected':
        key = {'state': 'error', 'message': 'Проверьте подключение и FBS-склад.', 'code': 'connection'}
    elif expiry and expiry <= now:
        key = {'state': 'error', 'message': 'API-ключ истёк. Обновите ключ кабинета.', 'code': 'key_expired'}
    elif expiry and expiry <= now + timedelta(days=7):
        key = {
            'state': 'warning', 'message': 'API-ключ истекает в течение 7 дней. Подготовьте новый.',
            'code': 'key_expiring',
        }
    elif not expiry:
        key = {'state': 'unknown', 'message': 'Ozon не передал срок ключа; MAP его не угадывает.', 'code': ''}
    checked = profile.health_monitor_checked_at
    monitor_status = 'ok' if checked and now - checked < MONITOR_STALE_AFTER else 'unknown'
    # A stale persisted queue observation must never look like live worker proof.
    queue = queue if queue is not None else (monitor.get('queue', 'unknown') if monitor_status == 'ok' else 'unknown')
    return {
        'observed_at': now, 'monitor_checked_at': checked, 'monitor_status': monitor_status,
        'queue_status': queue, 'credential': key, 'jobs': jobs,
    }
