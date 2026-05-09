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

from apps.notifications.models import TenantNotificationSettings
from apps.notifications.telegram import TelegramNotifier

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Telegram long polling для локальной разработки (вместо webhook)'

    def handle(self, *args, **options):
        token = settings.TELEGRAM_BOT_TOKEN
        if not token:
            self.stderr.write('TELEGRAM_BOT_TOKEN не задан в .env')
            return

        # Сбрасываем webhook чтобы polling работал
        requests.post(f'https://api.telegram.org/bot{token}/deleteWebhook')
        self.stdout.write(self.style.SUCCESS('Polling запущен. Ctrl+C для остановки.'))

        offset = 0
        while True:
            try:
                resp = requests.get(
                    f'https://api.telegram.org/bot{token}/getUpdates',
                    params={'offset': offset, 'timeout': 30},
                    timeout=35,
                )
                updates = resp.json().get('result', [])
            except requests.RequestException as exc:
                self.stderr.write(f'Ошибка getUpdates: {exc}')
                time.sleep(5)
                continue

            for update in updates:
                offset = update['update_id'] + 1
                self._handle_update(update)

    def _handle_update(self, update: dict) -> None:
        message = update.get('message') or update.get('edited_message')
        if not message:
            return

        text = message.get('text', '').strip()
        chat = message.get('chat', {})
        chat_id = str(chat.get('id', ''))
        username = chat.get('username', '') or chat.get('first_name', '')

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
        self.stdout.write(f'Получен /start с токеном от chat_id={chat_id}')

        try:
            ns = TenantNotificationSettings.objects.select_related('tenant').get(
                connect_token=token,
            )
        except TenantNotificationSettings.DoesNotExist:
            TelegramNotifier().send(chat_id, '❌ Ссылка недействительна или устарела.')
            return

        if not ns.is_connect_token_valid(token):
            TelegramNotifier().send(chat_id, '❌ Срок действия ссылки истёк (15 мин). Сгенерируйте новую в настройках.')
            return

        ns.complete_telegram_connect(chat_id, username)
        self.stdout.write(self.style.SUCCESS(
            f'Telegram привязан: chat_id={chat_id}, tenant={ns.tenant.slug}'
        ))
        TelegramNotifier().send(
            chat_id,
            f'✅ Telegram подключён к организации <b>{ns.tenant.name}</b>.\n'
            f'Вы будете получать уведомления об ошибках и важных событиях.',
        )
