import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


class EmailNotifier:
    """
    Отправляет email-уведомления через настроенный Django mail backend.

    Использует защищённый platform SMTP backend из настроек Django.
    """

    def send(self, to: str, subject: str, body: str) -> bool:
        """
        Отправляет письмо на указанный адрес.

        Возвращает True при успехе, False при любой ошибке (не бросает исключений).
        """
        if not to:
            return False
        try:
            sent = send_mail(
                subject=subject,
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[to],
                fail_silently=False,
            )
            return sent == 1
        except Exception as exc:
            # Recipient addresses and provider exception text may contain PII
            # or credential-bearing connection details. Keep logs structural.
            logger.warning('Email send failed (%s).', type(exc).__name__)
            return False
