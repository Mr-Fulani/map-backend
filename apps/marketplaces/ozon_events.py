"""Transactional, deduplicated Ozon events without provider bodies or secrets."""

import hashlib
import json

from django.utils import timezone

from apps.sync.models import SyncLog


KIND_EVENTS = {
    'product_import': (SyncLog.EVENT_LISTING_PUBLISH, 'Публикация или обновление карточки'),
    'price_update': (SyncLog.EVENT_LISTING_PRICE_UPDATE, 'Обновление цены'),
    'stock_update': (SyncLog.EVENT_LISTING_STOCK_UPDATE, 'Обновление остатка'),
    'archive': (SyncLog.EVENT_LISTING_UNPUBLISH, 'Снятие с продажи'),
}
STATE_MESSAGES = {
    'queued': 'MAP принял запрос.',
    'reconciling': 'Проверяем результат в Ozon.',
    'outcome_unknown': 'Ответ не получен. MAP проверит результат; повторная отправка пока не нужна.',
    'succeeded': 'Ozon подтвердил выполнение.',
    'partial': 'Ozon выполнил запрос частично. Откройте карточку для проверки.',
    'failed': 'Операция не выполнена. Откройте карточку для проверки.',
    'manual_review': 'Нужна ручная проверка. Откройте карточку.',
}
ERROR_MESSAGES = {
    'write_disabled': 'Запись в Ozon выключена для этого кабинета.',
    'account_not_ready': 'Проверьте подключение кабинета Ozon.',
    'auth_error': 'Ozon отклонил API-ключ. Проверьте подключение кабинета.',
    'invalid_credentials': 'Ozon отклонил API-ключ. Проверьте подключение кабинета.',
    'connection_error': 'Не удалось связаться с Ozon. MAP повторит проверку позже.',
    'provider_unavailable': 'Ozon временно недоступен. MAP повторит проверку позже.',
    'rate_limited': 'Ozon ограничил частоту запросов. MAP повторит проверку позже.',
    'rate_limit': 'Ozon ограничил частоту запросов. MAP повторит проверку позже.',
    'timeout': 'Ozon не ответил вовремя. MAP повторит проверку позже.',
    'transport_error': 'Не удалось связаться с Ozon. MAP повторит проверку позже.',
    'warehouse_missing': 'Выберите склад в настройках кабинета Ozon.',
    'page_limit_exceeded': 'Заказов больше лимита одного обхода. Нужна проверка синхронизации.',
    'unexpected_error': 'Внутренняя ошибка синхронизации. MAP повторит проверку позже.',
}


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def record_operation_event(operation) -> None:
    from apps.marketplaces.models import OzonOfferDraft

    if operation.state not in STATE_MESSAGES or operation.kind not in KIND_EVENTS:
        return
    database = operation._state.db or 'default'
    # Check every relation before projecting an event into a tenant's journal.
    product_id = OzonOfferDraft.objects.using(database).filter(
        pk=operation.offer_id, tenant_id=operation.tenant_id,
        account_id=operation.account_id, account__tenant_id=operation.tenant_id,
        account__marketplace='ozon', product__tenant_id=operation.tenant_id,
    ).values_list('product_id', flat=True).first()
    if product_id is None:
        return
    event_type, label = KIND_EVENTS[operation.kind]
    summary = operation.response_summary if isinstance(operation.response_summary, dict) else {}
    warnings = bool(summary.get('provider_warnings')) and operation.state == 'succeeded'
    status_error = bool(summary.get('last_status_error')) and operation.state == 'reconciling'
    status = SyncLog.STATUS_OK
    if operation.state == 'failed':
        status = SyncLog.STATUS_ERROR
    elif operation.state in {'outcome_unknown', 'partial', 'manual_review'} or warnings or status_error:
        status = SyncLog.STATUS_WARN
    message = STATE_MESSAGES[operation.state]
    if warnings:
        message = 'Ozon подтвердил карточку с замечаниями. Откройте карточку для проверки.'
    elif status_error:
        message = 'Не удалось проверить статус в Ozon. MAP повторит проверку позже.'
    payload = {
        'ozon_operation_id': str(operation.pk),
        'kind': operation.kind, 'state': operation.state,
        'provider_warning': warnings, 'status_check_failed': status_error,
    }
    # Only local enums/identifiers reach the journal. Provider request/response
    # bodies and free-form exception messages never become public log payloads.
    SyncLog.objects.using(database).get_or_create(
        event_key=f'ozon:operation:{operation.pk}:{_digest(payload)}',
        defaults={
            'tenant_id': operation.tenant_id, 'account_id': operation.account_id,
            'product_id': product_id, 'event_type': event_type, 'status': status,
            'message': f'Ozon · {label}. {message}', 'payload': payload,
        },
    )


def record_automation_result(profile, job: str, *, checked: int, failed: int, code: str) -> None:
    """Persist latest health and emit one issue/recovery per state transition."""
    from apps.marketplaces.models import OzonAccountProfile

    safe_code = code if code in ERROR_MESSAGES else ('sync_failed' if failed else '')
    health = dict(profile.automation_health or {})
    previous = health.get(job, {})
    now = timezone.now()
    health[job] = {
        'checked_at': now.isoformat(), 'checked': checked,
        'failed': failed, 'error_code': safe_code,
    }
    updated = OzonAccountProfile.objects.filter(
        pk=profile.pk, account__tenant_id=profile.account.tenant_id,
        account__marketplace='ozon',
    ).update(automation_health=health)
    if not updated or safe_code == previous.get('error_code', ''):
        return
    labels = {
        'commerce': (SyncLog.EVENT_LISTING_UPDATE, 'Автосинхронизация цены и остатка'),
        'orders': (SyncLog.EVENT_ORDERS_SYNC, 'Получение заказов'),
        'reconciliation': (SyncLog.EVENT_MODERATION, 'Проверка статуса карточек'),
    }
    event_type, label = labels[job]
    detail = (
        ERROR_MESSAGES.get(safe_code, 'Синхронизация не завершена. Проверьте кабинет и карточки.')
        if failed else 'Синхронизация восстановлена.'
    )
    SyncLog.objects.create(
        tenant_id=profile.account.tenant_id, account_id=profile.account_id,
        event_type=event_type, status=SyncLog.STATUS_WARN if failed else SyncLog.STATUS_OK,
        message=f'Ozon · {label}. {detail}',
        payload={'job': job, 'checked': checked, 'failed': failed, 'error_code': safe_code},
    )
