import requests
from django.core.cache import cache

from apps.datasources.encryption import decrypt

TOKEN_KEY = 'avito:token:{account_id}'
TOKEN_BUFFER_SECONDS = 300


class AvitoAuthManager:
    def get_token(self, account) -> str:
        token = cache.get(TOKEN_KEY.format(account_id=account.pk))
        if token:
            return token
        return self._refresh(account)

    def _refresh(self, account) -> str:
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
        ttl = data['expires_in'] - TOKEN_BUFFER_SECONDS
        cache.set(TOKEN_KEY.format(account_id=account.pk), data['access_token'], timeout=ttl)
        return data['access_token']

    def invalidate(self, account) -> None:
        cache.delete(TOKEN_KEY.format(account_id=account.pk))
