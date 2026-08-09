import requests
from django.conf import settings
from django.core.cache import cache

from apps.core.http_responses import bounded_http_request
from apps.datasources.encryption import decrypt

TOKEN_KEY = 'avito:token:{account_id}'
TOKEN_BUFFER_SECONDS = 300  # Буфер до истечения токена — не ждём последней секунды


class AvitoAuthManager:
    """Управляет OAuth-токенами Avito API per-account через Redis-кэш."""

    def get_token(self, account) -> str:
        """Возвращает токен из кэша или обновляет его через API."""
        token = cache.get(TOKEN_KEY.format(account_id=account.pk))
        if token:
            return token
        return self._refresh(account)

    def _refresh(self, account) -> str:
        """Запрашивает новый access_token через client_credentials flow."""
        creds = decrypt(account.credentials_enc)
        resp = bounded_http_request(
            requests.post,
            'https://api.avito.ru/token',
            data={
                'grant_type': 'client_credentials',
                'client_id': creds['client_id'],
                'client_secret': creds['client_secret'],
            },
            timeout=15,
            max_bytes=settings.AVITO_API_RESPONSE_MAX_BYTES,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError('Avito token endpoint вернул некорректный JSON-объект.')
        token = data.get('access_token')
        expires_in = data.get('expires_in')
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 8192
            or not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or not 1 <= expires_in <= 31 * 24 * 60 * 60
        ):
            raise ValueError('Avito token endpoint вернул некорректные credentials.')
        # Храним с TTL чуть меньше реального — чтобы не использовать просроченный токен.
        # При проверке новых credentials аккаунт ещё не сохранён, поэтому pk=None:
        # такой токен не должен попадать в общий cache.
        if account.pk is not None and expires_in > TOKEN_BUFFER_SECONDS:
            ttl = expires_in - TOKEN_BUFFER_SECONDS
            cache.set(TOKEN_KEY.format(account_id=account.pk), token, timeout=ttl)
        return token

    def invalidate(self, account) -> None:
        """Удаляет токен из кэша — вызывается при получении 401 от Avito."""
        cache.delete(TOKEN_KEY.format(account_id=account.pk))
