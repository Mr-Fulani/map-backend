import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


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
            resp = requests.post(
                url,
                json={'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning('Telegram API error %s: %s', resp.status_code, resp.text[:200])
                return False
            return True
        except requests.RequestException as exc:
            logger.warning('Telegram send failed: %s', exc)
            return False
