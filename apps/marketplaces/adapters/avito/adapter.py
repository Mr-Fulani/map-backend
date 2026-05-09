import requests

from apps.marketplaces.adapters.avito.auth import AvitoAuthManager
from apps.marketplaces.adapters.avito.error_handler import handle_avito_error
from apps.marketplaces.adapters.avito.rate_limiter import AvitoRateLimiter

AVITO_API_BASE = 'https://api.avito.ru'


class AvitoAdapter:
    """Адаптер для работы с Avito API: публикация, обновление, удаление объявлений."""

    def __init__(self, account):
        self.account = account
        self._auth = AvitoAuthManager()
        self._rl = AvitoRateLimiter()

    def _headers(self) -> dict:
        """Формирует заголовки с актуальным Bearer-токеном."""
        token = self._auth.get_token(self.account)
        return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    def _request(self, method: str, path: str, operation: str, **kwargs):
        """Выполняет запрос с rate limiting и авто-обновлением токена при 401."""
        self._rl.consume(self.account, operation)
        url = f'{AVITO_API_BASE}{path}'
        resp = getattr(requests, method)(url, headers=self._headers(), timeout=30, **kwargs)
        self._rl.handle_response_headers(dict(resp.headers), self.account)
        if resp.status_code == 401:
            # Токен мог протухнуть между вызовами — инвалидируем и повторяем один раз
            self._auth.invalidate(self.account)
            resp = getattr(requests, method)(url, headers=self._headers(), timeout=30, **kwargs)
        handle_avito_error(resp)
        return resp

    def publish(self, listing) -> str:
        """Публикует объявление на Avito. Возвращает external_id."""
        payload = {
            'category_id': listing.product.category_1c,
            'title': listing.title,
            'description': listing.description_ai,
            'price': int(listing.price_on_listing),
            'idempotency_key': str(listing.publish_idempotency_key),
        }
        resp = self._request('post', f'/core/v1/accounts/{self.account.external_id}/items',
                             'publish', json=payload)
        return resp.json()['id']

    def update(self, listing) -> None:
        """Обновляет контент объявления (заголовок и описание)."""
        payload = {
            'title': listing.title,
            'description': listing.description_ai,
        }
        self._request('put',
                      f'/core/v1/accounts/{self.account.external_id}/items/{listing.external_id}',
                      'update', json=payload)

    def update_price(self, listing) -> None:
        """Обновляет только цену — минимальный PATCH-запрос."""
        payload = {'price': int(listing.price_on_listing)}
        self._request('patch',
                      f'/core/v1/accounts/{self.account.external_id}/items/{listing.external_id}',
                      'price', json=payload)

    def unpublish(self, listing) -> None:
        """Снимает объявление с публикации (архивирует)."""
        self._request('post',
                      f'/core/v1/accounts/{self.account.external_id}/items/{listing.external_id}/stop',
                      'delete')

    def delete(self, listing) -> None:
        """Удаляет объявление из Avito."""
        self._request('delete',
                      f'/core/v1/accounts/{self.account.external_id}/items/{listing.external_id}',
                      'delete')

    def get_status(self, listing) -> dict:
        """Запрашивает текущий статус объявления у Avito."""
        resp = self._request('get',
                             f'/core/v1/accounts/{self.account.external_id}/items/{listing.external_id}',
                             'update')
        return resp.json()
