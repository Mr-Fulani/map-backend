"""
Management command для локальной разработки.

Вместо webhook использует long polling (getUpdates),
обрабатывает /start <token> так же, как TelegramBotWebhookView.
Запускать только локально — на проде используется webhook.
"""

import time
import logging

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.core.http_responses import trusted_api_max_bytes
from apps.notifications.models import (
    CONNECT_TOKEN_CONSUMED,
    CONNECT_TOKEN_EXPIRED,
    TenantNotificationSettings,
)
from apps.notifications.telegram import TelegramNotifier
from apps.notifications.telegram_api import (
    TelegramAPIError,
    expect_boolean_result,
    request_telegram_json,
)

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5.0
CONTROL_READ_TIMEOUT_SECONDS = 10.0
CONTROL_TOTAL_DEADLINE_SECONDS = 15.0
POLL_READ_TIMEOUT_SECONDS = 35.0
POLL_TOTAL_DEADLINE_SECONDS = 40.0
CONTROL_RESPONSE_MAX_BYTES = 64 * 1024
UPDATES_RESPONSE_MAX_BYTES = 1024 * 1024


class Command(BaseCommand):
    help = 'Telegram long polling для локальной разработки (вместо webhook)'

    def handle(self, *args, **options):
        token = settings.TELEGRAM_BOT_TOKEN
        if not token:
            self.stderr.write('TELEGRAM_BOT_TOKEN не задан в .env')
            return

        # Сбрасываем webhook чтобы polling работал
        base = f'https://api.telegram.org/bot{token}'
        try:
            payload = self._request_control(
                requests.post,
                f'{base}/deleteWebhook',
            )
            expect_boolean_result(payload)
        except TelegramAPIError as exc:
            self.stderr.write(f'Ошибка deleteWebhook: {exc}')
            return
        self.stdout.write(self.style.SUCCESS('Polling запущен. Ctrl+C для остановки.'))

        offset = 0
        while True:
            try:
                payload = self._request_updates(
                    requests.get,
                    f'{base}/getUpdates',
                    params={'offset': offset, 'timeout': 30},
                )
                updates = self._validate_updates(payload.get('result'))
            except TelegramAPIError as exc:
                self.stderr.write(f'Ошибка getUpdates: {exc}')
                time.sleep(5)
                continue

            for update in updates:
                offset = update['update_id'] + 1
                self._handle_update(update)

    @staticmethod
    def _validate_updates(result) -> list[dict]:
        if not isinstance(result, list):
            raise TelegramAPIError('getUpdates returned an invalid result.')
        for update in result:
            if not isinstance(update, dict):
                raise TelegramAPIError('getUpdates returned a non-object update.')
            update_id = update.get('update_id')
            if (
                not isinstance(update_id, int)
                or isinstance(update_id, bool)
                or update_id < 0
            ):
                raise TelegramAPIError('getUpdates returned an invalid update_id.')
            for message_key in ('message', 'edited_message'):
                message = update.get(message_key)
                if message is not None and not isinstance(message, dict):
                    raise TelegramAPIError(
                        f'getUpdates returned an invalid {message_key}.'
                    )
        return result

    @staticmethod
    def _request_control(requester, url: str, **kwargs) -> dict:
        return request_telegram_json(
            requester,
            url,
            timeout=(CONNECT_TIMEOUT_SECONDS, CONTROL_READ_TIMEOUT_SECONDS),
            max_elapsed_seconds=CONTROL_TOTAL_DEADLINE_SECONDS,
            max_bytes=min(
                trusted_api_max_bytes(settings),
                CONTROL_RESPONSE_MAX_BYTES,
            ),
            **kwargs,
        )

    @staticmethod
    def _request_updates(requester, url: str, **kwargs) -> dict:
        return request_telegram_json(
            requester,
            url,
            timeout=(CONNECT_TIMEOUT_SECONDS, POLL_READ_TIMEOUT_SECONDS),
            max_elapsed_seconds=POLL_TOTAL_DEADLINE_SECONDS,
            max_bytes=min(
                trusted_api_max_bytes(settings),
                UPDATES_RESPONSE_MAX_BYTES,
            ),
            **kwargs,
        )

    def _handle_update(self, update: dict) -> None:
        message = update.get('message') or update.get('edited_message')
        if not isinstance(message, dict):
            return

        raw_text = message.get('text')
        chat = message.get('chat')
        if not isinstance(raw_text, str) or not isinstance(chat, dict):
            return
        raw_chat_id = chat.get('id')
        if not isinstance(raw_chat_id, int) or isinstance(raw_chat_id, bool):
            return

        username = chat.get('username') or chat.get('first_name') or ''
        if not isinstance(username, str):
            return
        text = raw_text.strip()
        chat_id = str(raw_chat_id)
        username = username[:100]

        if not text.startswith('/start'):
            return

        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            TelegramNotifier().send(
                chat_id,
                '👋 Это бот MAP. Для привязки перейдите в настройки уведомлений и нажмите «Подключить Telegram».',
            )
            return

        token = parts[1].strip()
        if not token or len(token) > 64:
            TelegramNotifier().send(chat_id, '❌ Ссылка недействительна или устарела.')
            return
        self.stdout.write(f'Получен /start с токеном от chat_id={chat_id}')

        ns, consume_status = TenantNotificationSettings.consume_connect_token(
            token,
            chat_id=chat_id,
            username=username,
        )
        if consume_status == CONNECT_TOKEN_EXPIRED:
            TelegramNotifier().send(chat_id, '❌ Срок действия ссылки истёк (15 мин). Сгенерируйте новую в настройках.')
            return
        if consume_status != CONNECT_TOKEN_CONSUMED or ns is None:
            TelegramNotifier().send(chat_id, '❌ Ссылка недействительна или устарела.')
            return
        self.stdout.write(self.style.SUCCESS(
            f'Telegram привязан: chat_id={chat_id}, tenant={ns.tenant.slug}'
        ))
        TelegramNotifier().send(
            chat_id,
            f'✅ Telegram подключён к организации <b>{ns.tenant.name}</b>.\n'
            f'Вы будете получать уведомления об ошибках и важных событиях.',
        )
