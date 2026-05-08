import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


class EmailNotifier:
    """
    Отправляет email-уведомления через настроенный Django mail backend.

    Использует SendPulse SMTP из настроек (EMAIL_HOST, EMAIL_HOST_USER и т.д.).
    """

    def send(self, to: str, subject: str, body: str) -> bool:
        """
        Отправляет письмо на указанный адрес.

        Возвращает True при успехе, False при любой ошибке (не бросает исключений).
        """
        if not to:
            return False
        try:
            send_mail(
                subject=subject,
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[to],
                fail_silently=False,
            )
            return True
        except Exception as exc:
            logger.warning('Email send failed to %s: %s', to, exc)
            return False
