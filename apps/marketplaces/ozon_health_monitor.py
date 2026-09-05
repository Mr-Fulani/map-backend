"""Bounded monitoring and durable, tenant-scoped incident notifications."""

from datetime import timedelta
from html import escape
import time
import uuid

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.core.advisory_lock import try_session_advisory_lock
from apps.core.dispatch import enqueue_durable_task
from apps.core.queue_observability import get_cached_celery_queue_snapshot
from apps.marketplaces.models import OzonAccountProfile
from apps.marketplaces.ozon_health import as_time, health_snapshot, profile_queryset, queue_status, work_snapshot
from apps.notifications.models import NotificationDelivery, TenantNotificationSettings
from apps.notifications.services import NotificationDeliveryError, NotificationService
from apps.sync.models import SyncLog


BATCH_SIZE = 100
RUN_SECONDS = 10
NOTICE_COOLDOWN = timedelta(minutes=30)
RECOVERY_GRACE = timedelta(minutes=2)
DELIVERY_TASK = 'apps.marketplaces.ozon_tasks.deliver_ozon_health_alert'


def _enqueue_notice(profile, state, message, *, recovery_from=None):
    event_key = f'ozon-health:{uuid.uuid4()}'
    state['notification_key'] = event_key
    # Immutable text is captured once: a retry cannot change the delivery fingerprint.
    body = f'Ozon · {escape(profile.account.name[:200])}\n{message}\nОткройте MAP → Настройки → Маркетплейсы → Ozon.'
    SyncLog.objects.create(
        tenant_id=profile.account.tenant_id, account_id=profile.account_id,
        event_key=event_key, event_type=SyncLog.EVENT_MODERATION,
        status=SyncLog.STATUS_OK if recovery_from else SyncLog.STATUS_WARN,
        message=f'Ozon · {message}', payload={'health_event': 'recovery' if recovery_from else 'incident'},
    )
    enqueue_durable_task(
        DELIVERY_TASK,
        args=[profile.account.tenant_id, profile.account_id, event_key, body, recovery_from],
        deduplication_key=event_key, max_run_attempts=6, execution_timeout_seconds=90,
    )
    return event_key


def observe_profile(profile, *, queue, now):
    """Caller owns the profile row lock and transaction, never a provider call."""
    state = dict(profile.health_monitor_state or {})
    work = work_snapshot(profile)
    watch = dict(state.get('watch_since') or {})
    enabled = {
        'commerce': profile.commerce_auto_sync_enabled and profile.product_write_enabled,
        'orders': profile.orders_auto_sync_enabled, 'reconciliation': True,
    }
    for job in enabled:
        if enabled[job] and work[job]:
            watch.setdefault(job, now.isoformat())
        else:
            watch.pop(job, None)
    state.update(work=work, queue=queue, watch_since=watch)
    profile.health_monitor_state = state
    snapshot = health_snapshot(profile, work=work, queue=queue, now=now)
    failing = [row for row in snapshot['jobs'] if row['alert_ready']]
    credential = snapshot['credential']['code']
    incident = state.get('incident')
    if failing or credential:
        incident = incident or {'opened_at': now.isoformat(), 'jobs': [], 'credential': False, 'alert_key': None}
        incident['jobs'] = sorted(set(incident['jobs']) | {row['job'] for row in failing})
        incident['credential'] = incident['credential'] or bool(credential)
        incident.pop('clear_since', None)
        last_notice = as_time(state.get('last_notice_at'))
        if not incident['alert_key'] and (last_notice is None or now - last_notice >= NOTICE_COOLDOWN):
            details = [f"{row['label']}: {row['message']}" for row in failing]
            if credential:
                details.insert(0, snapshot['credential']['message'])
            incident['alert_key'] = _enqueue_notice(profile, state, '\n'.join(details))
            state['last_notice_at'] = now.isoformat()
        state['incident'] = incident
    elif incident:
        opened = as_time(incident['opened_at']) or now
        jobs = {row['job']: row for row in snapshot['jobs']}
        confirmed = all(
            jobs[job]['state'] in {'ok', 'idle'}
            and jobs[job]['last_success_at'] and jobs[job]['last_success_at'] >= opened
            for job in incident['jobs']
        ) and not any(row['state'] in {'error', 'delayed'} for row in jobs.values()) and (
            not incident['credential'] or bool(profile.last_checked_at and profile.last_checked_at >= opened)
        )
        # A disabled switch or an empty queue is not evidence of recovery.
        quiet_close = any(jobs[job]['state'] in {'disabled', 'idle'} for job in incident['jobs'])
        if confirmed:
            clear_since = as_time(incident.get('clear_since')) or now
            incident['clear_since'] = clear_since.isoformat()
            if now - clear_since >= RECOVERY_GRACE:
                if incident['alert_key']:
                    _enqueue_notice(
                        profile, state, 'Работа кабинета восстановлена. Проверки снова проходят успешно.',
                        recovery_from=incident['alert_key'],
                    )
                state['incident'] = None
        elif quiet_close:
            state['incident'] = None
            state['notification_key'] = None
        else:
            incident.pop('clear_since', None)
    OzonAccountProfile.objects.filter(pk=profile.pk).update(
        health_monitor_checked_at=now, health_monitor_state=state,
    )


def monitor_accounts():
    started = time.monotonic()
    checked = 0
    with try_session_advisory_lock('ozon:health-monitor') as acquired:
        if not acquired:
            return {'checked': 0, 'busy': True}
        now = timezone.now()
        due = profile_queryset().filter(account__is_active=True, account__tenant__is_active=True).filter(
            Q(health_monitor_checked_at__isnull=True)
            | Q(health_monitor_checked_at__lte=now - timedelta(seconds=50))
        )
        ids = list(due.order_by(
            F('health_monitor_checked_at').asc(nulls_first=True), 'pk',
        ).values_list('pk', flat=True)[:BATCH_SIZE])
        queue = queue_status(get_cached_celery_queue_snapshot())
        for pk in ids:
            if time.monotonic() - started >= RUN_SECONDS:
                break
            with transaction.atomic():
                profile = due.select_for_update(of=('self',)).filter(pk=pk).first()
                if profile is None:
                    continue
                observe_profile(profile, queue=queue, now=timezone.now())
                checked += 1
    return {'checked': checked, 'busy': False}


def deliver_alert(tenant_id, account_id, event_key, message, recovery_from=None):
    profile = profile_queryset().filter(
        account_id=account_id, account__tenant_id=tenant_id,
        account__is_active=True, account__tenant__is_active=True,
    ).first()
    if profile is None or profile.health_monitor_state.get('notification_key') != event_key:
        return {'status': 'superseded'}
    current = health_snapshot(profile, work=work_snapshot(profile))
    problem = current['credential']['code'] or any(row['alert_ready'] for row in current['jobs'])
    if (not recovery_from and not problem) or (recovery_from and (
        problem or any(row['state'] in {'error', 'delayed'} for row in current['jobs'])
    )):
        return {'status': 'superseded'}
    ns = TenantNotificationSettings.objects.filter(tenant_id=tenant_id).first()
    # Respect the error preference on recovery too; legacy LEVEL_SUCCESS has no mute check.
    if ns is None or not ns.notify_on_error or not ns.telegram_chat_id:
        return {'status': 'muted_or_unconfigured'}
    if recovery_from and not NotificationDelivery.objects.filter(
        tenant_id=tenant_id, event_key=recovery_from, status='sent', channel='telegram',
    ).exists():
        return {'status': 'no_delivered_incident'}
    try:
        NotificationService().notify(
            profile.account.tenant, 'success' if recovery_from else 'error',
            message, event_key=event_key,
        )
    except NotificationDeliveryError as exc:
        if exc.retryable:
            raise RuntimeError('Ozon health notification needs a safe delivery retry.') from None
        return {'status': 'delivery_needs_review'}
    return {'status': 'delivered'}
