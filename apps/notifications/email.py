import logging
import re
from datetime import datetime, timezone
from email.utils import format_datetime
import hashlib

from django.conf import settings
from django.core.mail import EmailMessage

logger = logging.getLogger(__name__)
_IDEMPOTENCY_KEY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$')


class EmailNotifier:
    """
    Отправляет email-уведомления через настроенный Django mail backend.

    Использует защищённый platform SMTP backend из настроек Django.
    """

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        idempotency_key: str,
        message_date: datetime,
    ) -> bool:
        """
        Отправляет письмо на указанный адрес.

        Возвращает True при успехе, False при любой ошибке (не бросает исключений).
        """
        if (
            not to
            or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None
            or not isinstance(message_date, datetime)
            or message_date.tzinfo is None
        ):
            return False
        stable_date = message_date.astimezone(timezone.utc).replace(microsecond=0)
        message_digest = hashlib.sha256(idempotency_key.encode('ascii')).hexdigest()
        sender_domain = settings.DEFAULT_FROM_EMAIL.rsplit('@', 1)[-1].lower()
        try:
            message = EmailMessage(
                subject=subject,
                body=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[to],
                headers={
                    'Resend-Idempotency-Key': idempotency_key,
                    'Date': format_datetime(stable_date),
                    'Message-ID': f'<map-{message_digest[:40]}@{sender_domain}>',
                },
            )
            sent = message.send(fail_silently=False)
            return sent == 1
        except Exception as exc:
            # Recipient addresses and provider exception text may contain PII
            # or credential-bearing connection details. Keep logs structural.
            logger.warning('Email send failed (%s).', type(exc).__name__)
            return False
