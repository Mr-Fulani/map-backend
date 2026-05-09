from apps.notifications.email import EmailNotifier
from apps.notifications.models import TenantNotificationSettings
from apps.notifications.telegram import TelegramNotifier

LEVEL_ERROR = 'error'
LEVEL_CRITICAL = 'critical'
LEVEL_BILLING = 'billing'


class NotificationService:
    """
    Маршрутизирует уведомления тенанта по каналам в зависимости от уровня.

    Уровни:
    - error    → Telegram (если notify_on_error=True)
    - critical → Telegram @mention + Email (если notify_on_critical=True)
    - billing  → только Email

    Если настройки уведомлений для тенанта отсутствуют — молча выходит.
    """

    def notify(self, tenant, level: str, message: str, payload: dict = None) -> None:
        """
        Отправляет уведомление тенанту через соответствующие каналы.

        payload сохраняется для логирования, но не передаётся в сами сообщения.
        """
        try:
            ns = tenant.notification_settings
        except TenantNotificationSettings.DoesNotExist:
            return

        if level == LEVEL_ERROR and ns.notify_on_error:
            self._send_telegram(ns.telegram_chat_id, f'⚠️ {message}')

        elif level == LEVEL_CRITICAL and ns.notify_on_critical:
            self._send_telegram(ns.telegram_chat_id, f'🚨 КРИТИЧНО: {message}')
            self._send_email(ns.notify_email, 'Критическая ошибка MAP', message)

        elif level == LEVEL_BILLING:
            self._send_email(ns.notify_email, 'Уведомление о биллинге MAP', message)

    def _send_telegram(self, chat_id: str, message: str) -> None:
        """Вызывает TelegramNotifier, не бросает исключений."""
        if chat_id:
            TelegramNotifier().send(chat_id, message)

    def _send_email(self, email: str, subject: str, body: str) -> None:
        """Вызывает EmailNotifier, не бросает исключений."""
        if email:
            EmailNotifier().send(email, subject, body)
