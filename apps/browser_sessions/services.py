"""
Сервисный слой: инициализация BrowserSession, переключение метода публикации.
Не содержит async-кода — только Django ORM и синхронный Celery.
"""

import logging
import os

from apps.browser_sessions.models import BrowserSession
from apps.browser_sessions.stealth import generate_fingerprint
from apps.datasources.encryption import encrypt
from apps.marketplaces.models import MarketplaceAccount

logger = logging.getLogger(__name__)

_PROFILE_BASE_DIR = os.environ.get('BROWSER_PROFILE_DIR', '/app/browser_profiles')


class BrowserSessionService:
    """Управляет созданием и переключением BrowserSession."""

    @staticmethod
    def initialize(account: MarketplaceAccount, login: str, password: str) -> BrowserSession:
        """
        Переводит аккаунт в web-режим: сохраняет credentials, создаёт BrowserSession,
        запускает фоновый логин.
        Возвращает BrowserSession (session_valid=False пока задача не завершится).
        """
        # Сохраняем credentials зашифрованно
        account.web_login_enc = encrypt({'value': login})
        account.web_password_enc = encrypt({'value': password})
        account.publish_method = MarketplaceAccount.PUBLISH_WEB
        account.save(update_fields=['web_login_enc', 'web_password_enc', 'publish_method'])

        # Создаём или обновляем BrowserSession
        browser_type = BrowserSession.browser_for_account(account.pk)
        fingerprint = generate_fingerprint(account.pk)
        profile_dir = os.path.join(_PROFILE_BASE_DIR, str(account.pk))

        session, _ = BrowserSession.objects.update_or_create(
            account=account,
            defaults={
                'browser_type': browser_type,
                'profile_dir': profile_dir,
                'fingerprint': fingerprint,
                'session_valid': False,
                'sms_pending': False,
            },
        )

        # Запускаем фоновый логин
        from apps.browser_sessions.tasks import start_browser_login_task
        start_browser_login_task.delay(account.pk)

        logger.info('browser_sessions: инициализирован web-режим для account=%s', account.pk)
        return session

    @staticmethod
    def switch_to_autoload(account: MarketplaceAccount) -> None:
        """Переводит аккаунт обратно на Autoload, удаляет BrowserSession и credentials."""
        account.publish_method = MarketplaceAccount.PUBLISH_AUTOLOAD
        account.web_login_enc = None
        account.web_password_enc = None
        account.save(update_fields=['publish_method', 'web_login_enc', 'web_password_enc'])

        BrowserSession.objects.filter(account=account).delete()
        logger.info('browser_sessions: account=%s переключён на autoload', account.pk)

    @staticmethod
    def get_web_credentials(account: MarketplaceAccount) -> tuple[str, str]:
        """Расшифровывает и возвращает (login, password) для повторного логина."""
        from apps.datasources.encryption import decrypt
        login = decrypt(bytes(account.web_login_enc))['value']
        password = decrypt(bytes(account.web_password_enc))['value']
        return login, password
