from apps.notifications.email import EmailNotifier
from apps.notifications.models import TenantNotificationSettings
from apps.notifications.telegram import TelegramNotifier

LEVEL_ERROR = 'error'
LEVEL_CRITICAL = 'critical'
LEVEL_BILLING = 'billing'
LEVEL_SUCCESS = 'success'
_LEVELS = frozenset({LEVEL_ERROR, LEVEL_CRITICAL, LEVEL_BILLING, LEVEL_SUCCESS})


class NotificationDeliveryError(RuntimeError):
    """One or more configured notification channels failed to deliver."""


class NotificationService:
    """
    Маршрутизирует уведомления тенанта по каналам в зависимости от уровня.

    Уровни:
    - error    → Telegram (если notify_on_error=True)
    - success  → Telegram (если Telegram привязан)
    - critical → Telegram @mention + Email (если notify_on_critical=True)
    - billing  → только Email

    Если настройки уведомлений для тенанта отсутствуют — молча выходит.
    """

    def notify(self, tenant, level: str, message: str, payload: dict = None) -> None:
        """
        Отправляет уведомление тенанту через соответствующие каналы.

        payload сохраняется для логирования, но не передаётся в сами сообщения.
        """
        if level not in _LEVELS:
            raise ValueError('Неизвестный уровень уведомления.')
        try:
            ns = tenant.notification_settings
        except TenantNotificationSettings.DoesNotExist:
            return

        failed_channels = []
        if level == LEVEL_ERROR and ns.notify_on_error:
            if not self._send_telegram(ns.telegram_chat_id, f'⚠️ {message}'):
                failed_channels.append('telegram')

        elif level == LEVEL_SUCCESS:
            if not self._send_telegram(ns.telegram_chat_id, f'✅ {message}'):
                failed_channels.append('telegram')

        elif level == LEVEL_CRITICAL and ns.notify_on_critical:
            if not self._send_telegram(ns.telegram_chat_id, f'🚨 КРИТИЧНО: {message}'):
                failed_channels.append('telegram')
            if not self._send_email(ns.notify_email, 'Критическая ошибка MAP', message):
                failed_channels.append('email')

        elif level == LEVEL_BILLING:
            if not self._send_email(ns.notify_email, 'Уведомление о биллинге MAP', message):
                failed_channels.append('email')

        if failed_channels:
            raise NotificationDeliveryError(
                'Не доставлены каналы: ' + ', '.join(failed_channels),
            )

    def _send_telegram(self, chat_id: str, message: str) -> bool:
        """Return success; an unconfigured channel is intentionally skipped."""
        return not chat_id or TelegramNotifier().send(chat_id, message)

    def _send_email(self, email: str, subject: str, body: str) -> bool:
        """Return success; an unconfigured channel is intentionally skipped."""
        return not email or EmailNotifier().send(email, subject, body)
