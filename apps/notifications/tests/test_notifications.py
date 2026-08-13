from datetime import timedelta
from unittest.mock import patch
import uuid

import pytest
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.test import override_settings
from django.utils.timezone import now

from apps.datasources.encryption import encrypt
from apps.marketplaces.models import MarketplaceAccount
from apps.notifications.email import EmailNotifier
from apps.notifications.models import NotificationDelivery, TenantNotificationSettings
from apps.notifications.services import (
    LEVEL_BILLING,
    LEVEL_CRITICAL,
    LEVEL_ERROR,
    LEVEL_SUCCESS,
    NotificationDeliveryError,
    NotificationService,
    mark_notification_event_exhausted,
)
from apps.notifications.tasks import cleanup_old_logs, send_notification_task
from apps.sync.models import SyncLog
from apps.tenants.services import TenantService


def make_tenant(slug):
    """Создаёт тенанта для тестов."""
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant


def make_notification_settings(tenant, telegram_chat_id='', notify_email='',
                               notify_on_error=True, notify_on_critical=True):
    """Создаёт TenantNotificationSettings для тенанта."""
    settings_row = tenant.notification_settings
    settings_row.telegram_chat_id = telegram_chat_id
    settings_row.notify_email = notify_email
    settings_row.notify_on_error = notify_on_error
    settings_row.notify_on_critical = notify_on_critical
    settings_row.save(update_fields=[
        'telegram_chat_id',
        'notify_email',
        'notify_on_error',
        'notify_on_critical',
    ])
    return settings_row


def make_account(tenant):
    """Создаёт MarketplaceAccount для тенанта."""
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        name='Test',
        external_id='1',
        credentials_enc=encrypt({'client_id': 'x', 'client_secret': 'y'}),
    )


@pytest.mark.django_db
class TestNotificationService:
    def test_error_sends_telegram(self):
        """При level=error вызывается TelegramNotifier, email не отправляется."""
        tenant = make_tenant('notif-err')
        make_notification_settings(tenant, telegram_chat_id='123456')

        with patch('apps.notifications.services.TelegramNotifier') as mock_tg, \
             patch('apps.notifications.services.EmailNotifier') as mock_email:
            mock_tg.return_value.send.return_value = True
            NotificationService().notify(tenant, LEVEL_ERROR, 'Ошибка публикации')

        mock_tg.return_value.send.assert_called_once()
        mock_email.return_value.send.assert_not_called()

    def test_critical_sends_telegram_and_email(self):
        """При level=critical отправляется и Telegram, и Email."""
        tenant = make_tenant('notif-crit')
        make_notification_settings(tenant, telegram_chat_id='123', notify_email='a@b.com')

        with patch('apps.notifications.services.TelegramNotifier') as mock_tg, \
             patch('apps.notifications.services.EmailNotifier') as mock_email:
            mock_tg.return_value.send.return_value = True
            mock_email.return_value.send.return_value = True
            NotificationService().notify(tenant, LEVEL_CRITICAL, 'Критично')

            mock_tg.return_value.send.assert_called_once()
            mock_email.return_value.send.assert_called_once()

    def test_success_sends_telegram_when_connected(self):
        """При level=success отправляется Telegram, если он привязан."""
        tenant = make_tenant('notify-success-co')
        make_notification_settings(tenant, telegram_chat_id='123456')

        with patch('apps.notifications.services.TelegramNotifier') as mock_tg, \
             patch('apps.notifications.services.EmailNotifier') as mock_email:
            NotificationService().notify(tenant, LEVEL_SUCCESS, 'Объявление опубликовано')

            mock_tg.return_value.send.assert_called_once()
            mock_email.return_value.send.assert_not_called()

    def test_success_skips_telegram_when_not_connected(self):
        """При level=success без Telegram ничего не отправляется."""
        tenant = make_tenant('notify-success-skip-co')
        make_notification_settings(tenant, telegram_chat_id='')

        with patch('apps.notifications.services.TelegramNotifier') as mock_tg:
            NotificationService().notify(tenant, LEVEL_SUCCESS, 'Объявление опубликовано')

            mock_tg.return_value.send.assert_not_called()

    def test_billing_sends_only_email(self):
        """При level=billing отправляется только Email, Telegram не вызывается."""
        tenant = make_tenant('notif-bill')
        make_notification_settings(tenant, telegram_chat_id='999', notify_email='pay@b.com')

        with patch('apps.notifications.services.TelegramNotifier') as mock_tg, \
             patch('apps.notifications.services.EmailNotifier') as mock_email:
            mock_email.return_value.send.return_value = True
            NotificationService().notify(tenant, LEVEL_BILLING, 'Счёт выставлен')

        mock_tg.return_value.send.assert_not_called()
        mock_email.return_value.send.assert_called_once()

    def test_notify_on_error_false_skips_telegram(self):
        """Если notify_on_error=False, Telegram при level=error не отправляется."""
        tenant = make_tenant('notif-skip-err')
        make_notification_settings(tenant, telegram_chat_id='123', notify_on_error=False)

        with patch('apps.notifications.services.TelegramNotifier') as mock_tg:
            NotificationService().notify(tenant, LEVEL_ERROR, 'Ошибка')

        mock_tg.return_value.send.assert_not_called()

    def test_no_settings_silently_exits(self):
        """Без TenantNotificationSettings notify() не падает и ничего не отправляет."""
        tenant = make_tenant('notif-none')
        TenantNotificationSettings.objects.filter(tenant=tenant).delete()
        tenant.refresh_from_db()

        with patch('apps.notifications.services.TelegramNotifier') as mock_tg, \
             patch('apps.notifications.services.EmailNotifier') as mock_email:
            NotificationService().notify(tenant, LEVEL_ERROR, 'Тест')

        mock_tg.return_value.send.assert_not_called()
        mock_email.return_value.send.assert_not_called()

    def test_failed_configured_channels_raise_after_all_are_attempted(self):
        tenant = make_tenant('notif-delivery-failure')
        make_notification_settings(
            tenant,
            telegram_chat_id='123',
            notify_email='ops@example.com',
        )

        with patch('apps.notifications.services.TelegramNotifier') as mock_tg, \
             patch('apps.notifications.services.EmailNotifier') as mock_email, \
             pytest.raises(NotificationDeliveryError, match='telegram, email'):
            mock_tg.return_value.send.return_value = False
            mock_email.return_value.send.return_value = False
            NotificationService().notify(tenant, LEVEL_CRITICAL, 'Критично')

        mock_tg.return_value.send.assert_called_once()
        mock_email.return_value.send.assert_called_once()

    def test_notification_task_uses_late_ack_and_bounded_transport_retries(self):
        assert send_notification_task.acks_late is True
        assert send_notification_task.reject_on_worker_lost is True
        assert send_notification_task.max_retries == 6

    def test_notification_task_does_not_retry_permanent_invalid_level(self):
        tenant = make_tenant('notif-permanent-error')

        with pytest.raises(ValueError, match='Неизвестный уровень'):
            send_notification_task(tenant.pk, 'not-a-level', 'bad producer payload')

    def test_redelivered_parent_reuses_event_key_across_new_child_task_ids(self):
        tenant = make_tenant('notif-parent-replay')
        observed = []
        with patch(
            'apps.notifications.services.NotificationService.notify',
            side_effect=lambda *args, **kwargs: observed.append(kwargs['event_key']),
        ):
            send_notification_task.push_request(
                id='child-a', parent_id='stable-parent-task',
            )
            try:
                send_notification_task(
                    tenant.pk, LEVEL_ERROR, 'same logical event', {'schema': 1},
                )
            finally:
                send_notification_task.pop_request()
            send_notification_task.push_request(
                id='child-b', parent_id='stable-parent-task',
            )
            try:
                send_notification_task(
                    tenant.pk, LEVEL_ERROR, 'same logical event', {'schema': 1},
                )
            finally:
                send_notification_task.pop_request()

        assert observed[0] == observed[1]

    def test_retry_of_partial_critical_delivery_skips_sent_telegram_channel(self):
        tenant = make_tenant('notif-partial-retry')
        make_notification_settings(
            tenant,
            telegram_chat_id='123',
            notify_email='ops@example.com',
        )

        with patch('apps.notifications.services.TelegramNotifier') as mock_tg, \
             patch('apps.notifications.services.EmailNotifier') as mock_email:
            mock_tg.return_value.send.return_value = True
            mock_email.return_value.send.side_effect = [False, True]
            with pytest.raises(NotificationDeliveryError) as first_error:
                NotificationService().notify(
                    tenant,
                    LEVEL_CRITICAL,
                    'Критично',
                    event_key='critical:event:1',
                )
            assert first_error.value.retryable is True

            NotificationService().notify(
                tenant,
                LEVEL_CRITICAL,
                'Критично',
                event_key='critical:event:1',
            )

        mock_tg.return_value.send.assert_called_once()
        assert mock_email.return_value.send.call_count == 2
        assert set(NotificationDelivery.objects.values_list('status', flat=True)) == {
            NotificationDelivery.Status.SENT,
        }

    def test_ambiguous_telegram_delivery_is_not_automatically_replayed(self):
        tenant = make_tenant('notif-telegram-uncertain')
        make_notification_settings(tenant, telegram_chat_id='123')

        with patch('apps.notifications.services.TelegramNotifier') as mock_tg:
            mock_tg.return_value.send.return_value = False
            with pytest.raises(NotificationDeliveryError) as first_error:
                NotificationService().notify(
                    tenant,
                    LEVEL_ERROR,
                    'Ошибка',
                    event_key='telegram:event:1',
                )
            assert first_error.value.retryable is False

            with pytest.raises(NotificationDeliveryError) as replay_error:
                NotificationService().notify(
                    tenant,
                    LEVEL_ERROR,
                    'Ошибка',
                    event_key='telegram:event:1',
                )
            assert replay_error.value.retryable is False

        mock_tg.return_value.send.assert_called_once()
        delivery = NotificationDelivery.objects.get()
        assert delivery.status == NotificationDelivery.Status.OUTCOME_UNCERTAIN

    def test_event_key_payload_conflict_fails_before_second_provider_call(self):
        tenant = make_tenant('notif-payload-conflict')
        make_notification_settings(tenant, notify_email='ops@example.com')

        with patch('apps.notifications.services.EmailNotifier') as mock_email:
            mock_email.return_value.send.return_value = True
            NotificationService().notify(
                tenant,
                LEVEL_BILLING,
                'Первый payload',
                event_key='billing:event:conflict',
            )
            with pytest.raises(ValueError, match='другим payload'):
                NotificationService().notify(
                    tenant,
                    LEVEL_BILLING,
                    'Другой payload',
                    event_key='billing:event:conflict',
                )

        mock_email.return_value.send.assert_called_once()

    def test_exhausted_retry_marks_pending_channel_for_manual_reconciliation(self):
        tenant = make_tenant('notif-retries-exhausted')
        delivery = NotificationDelivery.objects.create(
            tenant=tenant,
            event_key='email:exhausted',
            channel=NotificationDelivery.Channel.EMAIL,
            payload_fingerprint='e' * 64,
            status=NotificationDelivery.Status.PENDING,
        )

        assert mark_notification_event_exhausted(
            tenant_id=tenant.pk,
            event_key='email:exhausted',
        ) == 1
        delivery.refresh_from_db()
        assert delivery.status == NotificationDelivery.Status.OUTCOME_UNCERTAIN
        assert delivery.error_code == 'automatic_retries_exhausted'

    def test_synclog_error_triggers_notification(self):
        """Запись SyncLog(status=error) инициирует уведомление через send_notification_task."""
        tenant = make_tenant('notif-synclog')
        make_notification_settings(tenant, telegram_chat_id='777', notify_on_error=True)

        SyncLog.objects.create(
            tenant=tenant,
            event_type=SyncLog.EVENT_LISTING_ERROR,
            status=SyncLog.STATUS_ERROR,
            message='Ошибка публикации листинга',
        )

        with patch('apps.notifications.services.TelegramNotifier') as mock_tg:
            mock_tg.return_value.send.return_value = True
            send_notification_task(tenant.pk, LEVEL_ERROR, 'Ошибка публикации листинга')

        mock_tg.return_value.send.assert_called_once()


@pytest.mark.django_db
class TestCleanupOldLogs:
    def test_deletes_logs_older_than_90_days(self):
        """cleanup_old_logs удаляет записи старше 90 дней."""
        tenant = make_tenant('cleanup-co')

        old_log = SyncLog.objects.create(
            tenant=tenant, event_type=SyncLog.EVENT_LISTING_PUBLISH,
            status=SyncLog.STATUS_OK, message='Старый лог',
        )
        SyncLog.objects.filter(pk=old_log.pk).update(created_at=now() - timedelta(days=91))

        fresh_log = SyncLog.objects.create(
            tenant=tenant, event_type=SyncLog.EVENT_LISTING_PUBLISH,
            status=SyncLog.STATUS_OK, message='Свежий лог',
        )

        result = cleanup_old_logs()

        assert result['deleted'] == 1
        assert SyncLog.objects.filter(pk=fresh_log.pk).exists()
        assert not SyncLog.objects.filter(pk=old_log.pk).exists()

    def test_keeps_logs_younger_than_90_days(self):
        """cleanup_old_logs не трогает логи моложе 90 дней."""
        tenant = make_tenant('cleanup-keep')
        SyncLog.objects.create(
            tenant=tenant, event_type=SyncLog.EVENT_BILLING,
            status=SyncLog.STATUS_OK, message='Сохранить',
        )

        result = cleanup_old_logs()

        assert result['deleted'] == 0


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
def test_email_notifier_sets_resend_idempotency_header():
    message_date = now()
    assert EmailNotifier().send(
        'recipient@example.com',
        'Subject',
        'Body',
        idempotency_key='map-notification/1234',
        message_date=message_date,
    ) is True
    assert EmailNotifier().send(
        'recipient@example.com',
        'Subject',
        'Body',
        idempotency_key='map-notification/1234',
        message_date=message_date,
    ) is True
    assert len(mail.outbox) == 2
    assert mail.outbox[0].extra_headers == {
        'Resend-Idempotency-Key': 'map-notification/1234',
        'Date': mail.outbox[1].extra_headers['Date'],
        'Message-ID': mail.outbox[1].extra_headers['Message-ID'],
    }
    assert mail.outbox[0].message().as_bytes() == mail.outbox[1].message().as_bytes()


@pytest.mark.django_db
def test_uncertain_delivery_requires_exact_confirm_and_reconciles_idempotently():
    tenant = make_tenant('notification-reconcile')
    delivery = NotificationDelivery.objects.create(
        tenant=tenant,
        event_key='telegram:uncertain:1',
        channel=NotificationDelivery.Channel.TELEGRAM,
        payload_fingerprint='c' * 64,
        status=NotificationDelivery.Status.OUTCOME_UNCERTAIN,
    )
    with pytest.raises(CommandError, match='точно совпадать'):
        call_command(
            'reconcile_notification_delivery',
            str(delivery.pk),
            action='sent',
            note='provider dashboard verified',
            confirm=str(uuid.uuid4()),
        )

    call_command(
        'reconcile_notification_delivery',
        str(delivery.pk),
        action='sent',
        note='provider dashboard verified',
        confirm=str(delivery.pk),
    )
    call_command(
        'reconcile_notification_delivery',
        str(delivery.pk),
        action='sent',
        note='provider dashboard verified',
        confirm=str(delivery.pk),
    )
    delivery.refresh_from_db()
    assert delivery.status == NotificationDelivery.Status.SENT
    assert delivery.reconciliation_action == 'sent'


@pytest.mark.django_db
def test_unresolved_delivery_cannot_be_deleted_directly_or_by_tenant_cascade():
    tenant = make_tenant('notification-delete-protection')
    delivery = NotificationDelivery.objects.create(
        tenant=tenant,
        event_key='unresolved-delete',
        channel=NotificationDelivery.Channel.TELEGRAM,
        payload_fingerprint='d' * 64,
        status=NotificationDelivery.Status.OUTCOME_UNCERTAIN,
    )
    with pytest.raises(ProtectedError), transaction.atomic():
        delivery.delete()
    with pytest.raises(ProtectedError), transaction.atomic():
        tenant.delete()
    assert NotificationDelivery.objects.filter(pk=delivery.pk).exists()


@pytest.mark.django_db
class TestSyncLogAPI:
    def test_list_returns_tenant_logs(self, client):
        """GET /api/v1/logs/ возвращает только логи своего тенанта."""
        from apps.tenants.models import APIKey

        tenant = make_tenant('logs-api')
        api_key, raw_key = APIKey.generate(
            tenant, 'test', scopes=['sync:read'],
        )
        SyncLog.objects.create(
            tenant=tenant, event_type=SyncLog.EVENT_LISTING_PUBLISH,
            status=SyncLog.STATUS_OK, message='ok',
        )

        resp = client.get('/api/v1/logs/', HTTP_AUTHORIZATION=f'Bearer {raw_key}')
        assert resp.status_code == 200
        data = resp.json()
        assert data['status'] == 'ok'
        assert data['meta']['total'] == 1

    def test_filter_by_status(self, client):
        """Фильтр ?status=error возвращает только логи с ошибками."""
        from apps.tenants.models import APIKey

        tenant = make_tenant('logs-filter')
        api_key, raw_key = APIKey.generate(
            tenant, 'test', scopes=['sync:read'],
        )
        SyncLog.objects.create(
            tenant=tenant, event_type=SyncLog.EVENT_LISTING_PUBLISH,
            status=SyncLog.STATUS_OK, message='ok',
        )
        SyncLog.objects.create(
            tenant=tenant, event_type=SyncLog.EVENT_LISTING_ERROR,
            status=SyncLog.STATUS_ERROR, message='err',
        )

        resp = client.get('/api/v1/logs/?status=error', HTTP_AUTHORIZATION=f'Bearer {raw_key}')
        assert resp.status_code == 200
        data = resp.json()
        assert data['meta']['total'] == 1
        assert data['data'][0]['status'] == 'error'

    def test_tenant_isolation(self, client):
        """Тенант видит только свои логи, не чужие."""
        from apps.tenants.models import APIKey

        t1 = make_tenant('iso-logs-1')
        t2 = make_tenant('iso-logs-2')
        _, raw_key1 = APIKey.generate(
            t1, 'test', scopes=['sync:read'],
        )

        SyncLog.objects.create(
            tenant=t1, event_type=SyncLog.EVENT_BILLING,
            status=SyncLog.STATUS_OK, message='t1 log',
        )
        SyncLog.objects.create(
            tenant=t2, event_type=SyncLog.EVENT_BILLING,
            status=SyncLog.STATUS_OK, message='t2 log',
        )

        resp = client.get('/api/v1/logs/', HTTP_AUTHORIZATION=f'Bearer {raw_key1}')
        assert resp.json()['meta']['total'] == 1
