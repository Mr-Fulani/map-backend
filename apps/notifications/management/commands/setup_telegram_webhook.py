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
            resp = requests.post(f'{base}/deleteWebhook', timeout=10)
            data = resp.json()
            if data.get('ok'):
                self.stdout.write(self.style.SUCCESS('Webhook удалён.'))
            else:
                self.stderr.write(self.style.ERROR(f'Ошибка: {data}'))
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

        resp = requests.post(
            f'{base}/setWebhook',
            json={
                'url': webhook_url,
                'secret_token': secret_token,
                'allowed_updates': ['message'],
                'drop_pending_updates': True,
            },
            timeout=10,
        )
        data = resp.json()

        if data.get('ok'):
            self.stdout.write(self.style.SUCCESS(
                f'Webhook зарегистрирован: {webhook_url}'
            ))
            # Проверим что Telegram видит его
            info = requests.get(f'{base}/getWebhookInfo', timeout=10).json()
            result = info.get('result', {})
            self.stdout.write(f'  url              : {result.get("url")}')
            self.stdout.write(f'  pending_updates  : {result.get("pending_update_count", 0)}')
            last_err = result.get('last_error_message')
            if last_err:
                self.stdout.write(self.style.WARNING(f'  last_error       : {last_err}'))
        else:
            self.stderr.write(self.style.ERROR(f'Ошибка setWebhook: {data}'))
