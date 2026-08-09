"""
Management command для регистрации Telegram webhook.

Вызывать один раз после деплоя или смены домена/токена.
На проде используется webhook (в отличие от локального telegram_poll.py).

Пример:
    docker compose exec django python manage.py setup_telegram_webhook
"""

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.core.http_responses import trusted_api_max_bytes
from apps.notifications.telegram_api import (
    TelegramAPIError,
    expect_boolean_result,
    request_telegram_json,
)


CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 10.0
TOTAL_DEADLINE_SECONDS = 15.0
CONTROL_RESPONSE_MAX_BYTES = 64 * 1024


class Command(BaseCommand):
    """Регистрирует webhook в Telegram Bot API для текущего домена."""

    help = 'Регистрирует Telegram webhook (запускать после деплоя)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--delete',
            action='store_true',
            help='Удалить webhook вместо регистрации',
        )

    def handle(self, *args, **options):
        token = settings.TELEGRAM_BOT_TOKEN
        if not token:
            self.stderr.write(self.style.ERROR('TELEGRAM_BOT_TOKEN не задан в .env'))
            return

        base = f'https://api.telegram.org/bot{token}'

        if options['delete']:
            try:
                data = self._request(
                    requests.post,
                    f'{base}/deleteWebhook',
                )
                expect_boolean_result(data)
            except TelegramAPIError as exc:
                self.stderr.write(self.style.ERROR(f'Ошибка deleteWebhook: {exc}'))
                return
            self.stdout.write(self.style.SUCCESS('Webhook удалён.'))
            return

        site_url = getattr(settings, 'SITE_URL', '').rstrip('/')
        if not site_url:
            self.stderr.write(self.style.ERROR(
                'SITE_URL не задан в настройках (напр. https://dodugir.com)'
            ))
            return

        webhook_url = f'{site_url}/api/v1/notifications/webhook/telegram/'
        # Токен бота имеет вид "12345:ABCdef..." — берём часть после ":",
        # она всегда состоит только из A-Z a-z 0-9 (что допускает Telegram).
        # То же значение проверяется в TelegramBotWebhookView.
        secret_token = token.split(':')[-1][:32]

        try:
            data = self._request(
                requests.post,
                f'{base}/setWebhook',
                json={
                    'url': webhook_url,
                    'secret_token': secret_token,
                    'allowed_updates': ['message'],
                    'drop_pending_updates': True,
                },
            )
            expect_boolean_result(data)
            self.stdout.write(self.style.SUCCESS(
                f'Webhook зарегистрирован: {webhook_url}'
            ))
            # Проверим что Telegram видит его
            info = self._request(requests.get, f'{base}/getWebhookInfo')
            result = self._validate_webhook_info(info.get('result'))
            self.stdout.write(f'  url              : {result.get("url")}')
            self.stdout.write(f'  pending_updates  : {result.get("pending_update_count", 0)}')
            last_err = result.get('last_error_message')
            if last_err:
                self.stdout.write(self.style.WARNING(f'  last_error       : {last_err}'))
        except TelegramAPIError as exc:
            self.stderr.write(self.style.ERROR(f'Ошибка Telegram API: {exc}'))

    @staticmethod
    def _validate_webhook_info(result) -> dict:
        if not isinstance(result, dict):
            raise TelegramAPIError('getWebhookInfo returned an invalid result.')

        url = result.get('url')
        pending = result.get('pending_update_count')
        last_error = result.get('last_error_message')
        if not isinstance(url, str):
            raise TelegramAPIError('getWebhookInfo returned an invalid URL.')
        if (
            not isinstance(pending, int)
            or isinstance(pending, bool)
            or pending < 0
        ):
            raise TelegramAPIError(
                'getWebhookInfo returned an invalid pending update count.'
            )
        if last_error is not None and not isinstance(last_error, str):
            raise TelegramAPIError(
                'getWebhookInfo returned an invalid last error message.'
            )
        return result

    @staticmethod
    def _request(requester, url: str, **kwargs) -> dict:
        return request_telegram_json(
            requester,
            url,
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            max_elapsed_seconds=TOTAL_DEADLINE_SECONDS,
            max_bytes=min(
                trusted_api_max_bytes(settings),
                CONTROL_RESPONSE_MAX_BYTES,
            ),
            **kwargs,
        )
