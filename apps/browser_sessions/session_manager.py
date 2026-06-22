"""
Управление браузерными сессиями Avito через Playwright.

Двухфазный логин:
  Фаза 1: start_login() — вводит credentials, возвращает 'success' или 'awaiting_code'
  Фаза 2: submit_code() — тенант вводит SMS/email-код, завершаем вход
Между фазами браузер живёт внутри одной asyncio.run() задачи (Celery).
Код передаётся через Redis: submit_code() → Redis → ждущий цикл в задаче.
"""

import asyncio
import json
import logging
from functools import partial

from django.core.cache import cache
from django.utils.timezone import now

from apps.browser_sessions.pool import BrowserPool
from apps.browser_sessions.stealth import generate_fingerprint
from apps.datasources.encryption import decrypt, encrypt

logger = logging.getLogger(__name__)

_CODE_WAIT_TIMEOUT = 300   # секунд ожидания кода от тенанта
_CODE_POLL_INTERVAL = 1    # секунда между проверками Redis

# Redis-ключи для коммуникации между Celery-задачей и API
_KEY_STATUS = 'browser:login:{account_id}:status'   # 'running'|'awaiting_code'|'success'|'error'
_KEY_CODE = 'browser:login:{account_id}:code'        # код, введённый тенантом
_KEY_ERROR = 'browser:login:{account_id}:error'      # текст ошибки
_KEY_METHODS = 'browser:login:{account_id}:methods'  # доступные методы верификации

# -------------------------------------------------------------------
# Selectors Avito — верифицированы по реальному HTML (2026-06-12)
# -------------------------------------------------------------------
_LOGIN_URL = 'https://www.avito.ru/avito-care/oauth/login'
_SEL_PHONE = '[data-marker="login-form/login/input"]'
_SEL_PASSWORD = '[data-marker="login-form/password/input"]'
_SEL_SUBMIT = '[data-marker="login-form/submit"]'
# Следующие три — нужна верификация (экран SMS/email-кода):
_SEL_CODE_INPUT = '[data-marker="login-form/code/input"]'
_SEL_CONFIRM_CODE = '[data-marker="login-form/code/submit"]'
_SEL_SMS_BTN = '[data-marker="login-form/code/sms"]'
_SEL_EMAIL_BTN = '[data-marker="login-form/code/email"]'
# Верифицировано — появляется только у залогиненного пользователя:
_SEL_LOGGED_IN = '[data-marker="header/username-button"]'


class LoginError(Exception):
    """Ошибка при логине на Avito."""


class SessionManager:
    """Управляет полным циклом: логин, верификация, сохранение/восстановление cookies."""

    def __init__(self):
        self.pool = BrowserPool()

    # ------------------------------------------------------------------ #
    #  Публичный API                                                       #
    # ------------------------------------------------------------------ #

    async def login_with_verification(self, session, login: str, password: str) -> None:
        """
        Полный флоу логина: credentials → ожидание кода (если нужен) → cookies.
        Блокирует asyncio-поток до завершения (или таймаута верификации).
        Вызывается из Celery задачи через asyncio.run().
        """
        account_id = session.account_id
        loop = asyncio.get_event_loop()

        async with self.pool.acquire(session) as page:
            await self._set_status(account_id, 'running')
            try:
                result = await self._enter_credentials(page, login, password)

                if result == 'success':
                    await self._save_cookies(page, session)
                    await self._set_status(account_id, 'success')
                    return

                # verification_needed — ждём код от тенанта через Redis
                methods = await self._detect_verification_methods(page)
                await loop.run_in_executor(
                    None,
                    partial(cache.set, _KEY_METHODS.format(account_id=account_id),
                            json.dumps(methods), 360),
                )
                await self._set_status(account_id, 'awaiting_code')
                logger.info('browser_sessions: account=%s ожидает код (%s)', account_id, methods)

                code = await self._wait_for_code(account_id)
                if not code:
                    raise LoginError('Тенант не ввёл код подтверждения в течение 5 минут')

                await self._enter_code(page, code)
                await self._save_cookies(page, session)
                await self._set_status(account_id, 'success')

            except Exception as exc:
                logger.error('browser_sessions: ошибка логина account=%s: %s', account_id, exc)
                await self._set_status(account_id, 'error', str(exc))
                raise

    async def restore_session(self, session) -> bool:
        """
        Открывает avito.ru с сохранёнными cookies, проверяет что сессия живая.
        Возвращает True если сессия активна.
        """
        if not session.cookies_enc:
            return False
        async with self.pool.acquire(session) as page:
            await self._load_cookies(page, session)
            await page.goto('https://www.avito.ru/', wait_until='domcontentloaded', timeout=30000)
            logged_in = await page.query_selector(_SEL_LOGGED_IN) is not None
        self._update_session_valid(session, logged_in)
        return logged_in

    def invalidate(self, session) -> None:
        """Сбрасывает сессию: cookies удаляются, флаг valid=False."""
        session.cookies_enc = None
        session.session_valid = False
        session.sms_pending = False
        session.last_login_at = None
        session.save(update_fields=['cookies_enc', 'session_valid', 'sms_pending', 'last_login_at'])

    # ------------------------------------------------------------------ #
    #  Приватные методы — браузерная логика                               #
    # ------------------------------------------------------------------ #

    async def _enter_credentials(self, page, login: str, password: str) -> str:
        """
        Переходит на страницу логина, вводит телефон и пароль.
        Возвращает 'success' (обычный случай) или 'verification_needed' (редко).
        """
        await page.goto(_LOGIN_URL, wait_until='domcontentloaded', timeout=30000)
        await self.pool.human_delay(500, 1000)

        await page.wait_for_selector(_SEL_PHONE, timeout=10000)
        await page.fill(_SEL_PHONE, login)
        await self.pool.human_delay()

        await page.fill(_SEL_PASSWORD, password)
        await self.pool.human_delay()

        await page.click(_SEL_SUBMIT)

        # Ждём либо успешного входа (username-button), либо экрана верификации (code input)
        # Обычный логин: просто телефон+пароль → сразу username-button
        try:
            await page.wait_for_selector(
                f'{_SEL_LOGGED_IN}, {_SEL_CODE_INPUT}, {_SEL_SMS_BTN}',
                timeout=15000,
            )
        except Exception:
            raise LoginError(
                'Avito не ответил после входа — проверьте логин и пароль'
            )

        if await page.query_selector(_SEL_LOGGED_IN):
            return 'success'
        return 'verification_needed'

    async def _detect_verification_methods(self, page) -> list[str]:
        """Определяет доступные методы верификации (sms / email)."""
        methods = []
        if await page.query_selector(_SEL_SMS_BTN):
            methods.append('sms')
        if await page.query_selector(_SEL_EMAIL_BTN):
            methods.append('email')
        if not methods:
            methods = ['sms']  # fallback — Avito почти всегда предлагает SMS
        return methods

    async def _enter_code(self, page, code: str) -> None:
        """Вводит полученный код подтверждения."""
        await page.wait_for_selector(_SEL_CODE_INPUT, timeout=5000)
        await page.fill(_SEL_CODE_INPUT, code)
        await self.pool.human_delay()
        await page.click(_SEL_CONFIRM_CODE)
        await self.pool.human_delay(1000, 2000)

        try:
            await page.wait_for_selector(_SEL_LOGGED_IN, timeout=10000)
        except Exception:
            raise LoginError('Неверный код подтверждения или истёк срок действия')

    async def _save_cookies(self, page, session) -> None:
        """Сохраняет все cookies (включая HttpOnly) в BrowserSession."""
        context = page.context
        cookies = await context.cookies()
        encrypted = await asyncio.get_event_loop().run_in_executor(
            None, partial(encrypt, {'cookies': cookies}),
        )
        session.cookies_enc = encrypted
        session.session_valid = True
        session.sms_pending = False
        session.last_login_at = now()
        await asyncio.get_event_loop().run_in_executor(
            None,
            partial(session.save, update_fields=[
                'cookies_enc', 'session_valid', 'sms_pending', 'last_login_at',
            ]),
        )
        logger.info('browser_sessions: cookies сохранены для account=%s', session.account_id)

    async def _load_cookies(self, page, session) -> None:
        """Загружает сохранённые cookies в браузерный контекст."""
        data = await asyncio.get_event_loop().run_in_executor(
            None, partial(decrypt, bytes(session.cookies_enc)),
        )
        await page.context.add_cookies(data['cookies'])

    # ------------------------------------------------------------------ #
    #  Redis-коммуникация                                                  #
    # ------------------------------------------------------------------ #

    async def _set_status(self, account_id: int, status: str, error: str = '') -> None:
        """Обновляет статус логина в Redis."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            partial(cache.set, _KEY_STATUS.format(account_id=account_id), status, 3600),
        )
        if error:
            await loop.run_in_executor(
                None,
                partial(cache.set, _KEY_ERROR.format(account_id=account_id), error, 3600),
            )

    async def _wait_for_code(self, account_id: int) -> str | None:
        """Ждёт код из Redis до _CODE_WAIT_TIMEOUT секунд."""
        loop = asyncio.get_event_loop()
        key = _KEY_CODE.format(account_id=account_id)
        for _ in range(_CODE_WAIT_TIMEOUT):
            code = await loop.run_in_executor(None, partial(cache.get, key))
            if code:
                await loop.run_in_executor(None, partial(cache.delete, key))
                return code
            await asyncio.sleep(_CODE_POLL_INTERVAL)
        return None

    # ------------------------------------------------------------------ #
    #  Синхронные хелперы                                                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _update_session_valid(session, valid: bool) -> None:
        session.session_valid = valid
        session.save(update_fields=['session_valid'])

    @staticmethod
    def get_login_status(account_id: int) -> dict:
        """Возвращает текущий статус логина из Redis для API."""
        status = cache.get(_KEY_STATUS.format(account_id=account_id), 'idle')
        error = cache.get(_KEY_ERROR.format(account_id=account_id), '')
        raw_methods = cache.get(_KEY_METHODS.format(account_id=account_id))
        methods = json.loads(raw_methods) if raw_methods else []
        return {'status': status, 'error': error, 'methods': methods}

    @staticmethod
    def submit_code_to_redis(account_id: int, code: str) -> None:
        """Кладёт код тенанта в Redis — задача подхватит его из _wait_for_code."""
        cache.set(_KEY_CODE.format(account_id=account_id), code, 360)


session_manager = SessionManager()
