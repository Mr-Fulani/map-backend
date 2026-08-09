import logging

from django.conf import settings

import requests

from apps.core.http_responses import trusted_api_max_bytes
from apps.notifications.telegram_api import (
    TelegramAPIError,
    request_telegram_json,
)

logger = logging.getLogger(__name__)
_SEND_RESPONSE_MAX_BYTES = 64 * 1024


class TelegramNotifier:
    """
    Отправляет сообщения в Telegram через Bot API.

    Требует TELEGRAM_BOT_TOKEN в настройках.
    При пустом токене возвращает False без ошибки — режим без Telegram.
    """

    _BASE_URL = 'https://api.telegram.org/bot{token}/sendMessage'

    def send(self, chat_id: str, message: str) -> bool:
        """
        Отправляет текстовое сообщение в указанный чат.

        Возвращает True при успехе, False при любой ошибке (не бросает исключений).
        """
        token = settings.TELEGRAM_BOT_TOKEN
        if not token or not chat_id:
            return False

        url = self._BASE_URL.format(token=token)
        try:
            payload = request_telegram_json(
                requests.post,
                url,
                json={'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'},
                timeout=(5.0, 10.0),
                max_elapsed_seconds=15.0,
                max_bytes=min(
                    trusted_api_max_bytes(settings),
                    _SEND_RESPONSE_MAX_BYTES,
                ),
            )
            if not isinstance(payload.get('result'), dict):
                raise TelegramAPIError(
                    'Telegram sendMessage returned an invalid result.',
                )
            return True
        except TelegramAPIError as exc:
            logger.warning('Telegram send failed (%s).', type(exc).__name__)
            return False
