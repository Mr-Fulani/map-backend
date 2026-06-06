import requests
from django.core.cache import cache

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
        resp = requests.post(
            'https://api.avito.ru/token',
            data={
                'grant_type': 'client_credentials',
                'client_id': creds['client_id'],
                'client_secret': creds['client_secret'],
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        # Храним с TTL чуть меньше реального — чтобы не использовать просроченный токен.
        # При проверке новых credentials аккаунт ещё не сохранён, поэтому pk=None:
        # такой токен не должен попадать в общий cache.
        if account.pk is not None:
            ttl = data['expires_in'] - TOKEN_BUFFER_SECONDS
            cache.set(TOKEN_KEY.format(account_id=account.pk), data['access_token'], timeout=ttl)
        return data['access_token']

    def invalidate(self, account) -> None:
        """Удаляет токен из кэша — вызывается при получении 401 от Avito."""
        cache.delete(TOKEN_KEY.format(account_id=account.pk))
