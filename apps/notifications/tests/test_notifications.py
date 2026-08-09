from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils.timezone import now

from apps.datasources.encryption import encrypt
from apps.marketplaces.models import MarketplaceAccount
from apps.notifications.models import TenantNotificationSettings
from apps.notifications.services import (
    LEVEL_BILLING,
    LEVEL_CRITICAL,
    LEVEL_ERROR,
    LEVEL_SUCCESS,
    NotificationDeliveryError,
    NotificationService,
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
    return TenantNotificationSettings.objects.create(
        tenant=tenant,
        telegram_chat_id=telegram_chat_id,
        notify_email=notify_email,
        notify_on_error=notify_on_error,
        notify_on_critical=notify_on_critical,
    )


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

    def test_notification_task_uses_late_ack_and_unbounded_transport_retries(self):
        assert send_notification_task.acks_late is True
        assert send_notification_task.reject_on_worker_lost is True
        assert send_notification_task.max_retries is None

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
