import datetime
import logging

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

from apps.marketplaces.adapters.avito.auth import AvitoAuthManager
from apps.marketplaces.adapters.avito.error_handler import handle_avito_error
from apps.marketplaces.adapters.avito.feed_builder import build_feed
from apps.marketplaces.adapters.avito.rate_limiter import AvitoRateLimiter
from apps.marketplaces.base import BaseMarketplaceAdapter

logger = logging.getLogger(__name__)

AVITO_API_BASE = 'https://api.avito.ru'
_STATS_CHUNK = 200  # максимум item_ids за один запрос к Stats API


class FeedUploadError(Exception):
    """Не удалось загрузить фид на S3 или уведомить Avito."""


class AvitoAdapter(BaseMarketplaceAdapter):
    """
    Адаптер Avito. Feed-based: publish/update/unpublish/delete работают через Autoload XML.
    Операции с ценой и статусом — через REST API.

    Публикация объявлений на Avito:
      1. Генерируем XML-фид (Avito Autoload formatVersion=3).
      2. Загружаем на Yandex S3 по пути feeds/{account_id}/feed.xml.
      3. Вызываем POST /autoload/v1/upload — Avito скачивает файл с pre-configured URL.
      4. Через GET /autoload/v2/items/avito_ids сопоставляем наши ad_id с avito_id.

    Требования:
      - YC_S3_BUCKET, YC_S3_ACCESS_KEY, YC_S3_SECRET_KEY должны быть заданы в settings.
      - Профиль Autoload в Avito должен быть настроен (feed_url = URL нашего S3-файла).
        Настройка профиля: setup_autoload_profile().
    """

    def __init__(self, account):
        super().__init__(account)
        self._auth = AvitoAuthManager()
        self._rl = AvitoRateLimiter()

    # ------------------------------------------------------------------ #
    #  Вспомогательные методы                                             #
    # ------------------------------------------------------------------ #

    def _headers(self) -> dict:
        """Формирует заголовки с актуальным Bearer-токеном."""
        token = self._auth.get_token(self.account)
        return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    def _request(self, method: str, path: str, operation: str, **kwargs):
        """Выполняет REST-запрос с rate limiting и авто-обновлением токена при 401."""
        self._rl.consume(self.account, operation)
        url = f'{AVITO_API_BASE}{path}'
        resp = getattr(requests, method)(url, headers=self._headers(), timeout=30, **kwargs)
        self._rl.handle_response_headers(dict(resp.headers), self.account)
        if resp.status_code == 401:
            self._auth.invalidate(self.account)
            resp = getattr(requests, method)(url, headers=self._headers(), timeout=30, **kwargs)
        handle_avito_error(resp)
        return resp

    def _feed_s3_key(self) -> str:
        return f'feeds/{self.account.pk}/feed.xml'

    def _feed_public_url(self) -> str:
        bucket = settings.YC_S3_BUCKET
        cdn = getattr(settings, 'YC_CDN_DOMAIN', '')
        if cdn:
            return f'https://{cdn}/{self._feed_s3_key()}'
        return f'https://storage.yandexcloud.net/{bucket}/{self._feed_s3_key()}'

    def _s3_client(self):
        return boto3.client(
            's3',
            endpoint_url='https://storage.yandexcloud.net',
            region_name='ru-central1',
            aws_access_key_id=settings.YC_S3_ACCESS_KEY,
            aws_secret_access_key=settings.YC_S3_SECRET_KEY,
        )

    def _upload_to_s3(self, feed_bytes: bytes) -> str:
        """Загружает фид на S3 и возвращает публичный URL."""
        if not settings.YC_S3_BUCKET:
            raise FeedUploadError(
                'S3 не настроен: задайте YC_S3_BUCKET, YC_S3_ACCESS_KEY, YC_S3_SECRET_KEY'
            )
        try:
            self._s3_client().put_object(
                Bucket=settings.YC_S3_BUCKET,
                Key=self._feed_s3_key(),
                Body=feed_bytes,
                ContentType='application/xml',
                ACL='public-read',
            )
        except (BotoCoreError, ClientError) as exc:
            raise FeedUploadError(f'Ошибка загрузки фида на S3: {exc}') from exc
        return self._feed_public_url()

    def _trigger_autoload(self) -> None:
        """Уведомляет Avito о новом фиде через POST /autoload/v1/upload."""
        token = self._auth.get_token(self.account)
        resp = requests.post(
            f'{AVITO_API_BASE}/autoload/v1/upload',
            headers={'Authorization': f'Bearer {token}'},
            timeout=30,
        )
        if resp.status_code not in (200, 204):
            logger.warning(
                'autoload/v1/upload вернул %s для account=%s: %s',
                resp.status_code, self.account.pk, resp.text[:200],
            )

    # ------------------------------------------------------------------ #
    #  Feed-based операции (publish / update / unpublish / delete)        #
    # ------------------------------------------------------------------ #

    def publish(self, listing) -> None:
        """
        Ставит листинг в фид для публикации на Avito.

        Возвращает None — external_id придёт после обработки фида через get_feed_results().
        Используйте flush_feed([listing]) для немедленной загрузки.
        """
        self.flush_feed([listing])

    def update(self, listing) -> None:
        """Обновляет содержимое объявления через перезагрузку фида."""
        self.flush_feed([listing])

    def unpublish(self, listing) -> None:
        """Снимает объявление с публикации (статус Remove в фиде)."""
        self.flush_feed([listing])

    def delete(self, listing) -> None:
        """Удаляет объявление (статус Remove в фиде)."""
        self.flush_feed([listing])

    def flush_feed(self, listings: list) -> bool:
        """
        Генерирует XML-фид для переданного списка листингов, загружает на S3 и
        уведомляет Avito через POST /autoload/v1/upload.

        Используется как для одиночных операций, так и для батч-публикации.
        Лимит Avito: не чаще 1 раза в час через /autoload/v1/upload.

        Возвращает True при успехе.
        """
        feed_bytes = build_feed(listings)
        self._upload_to_s3(feed_bytes)
        self._trigger_autoload()
        return True

    # ------------------------------------------------------------------ #
    #  REST-операции (price / status / stats)                             #
    # ------------------------------------------------------------------ #

    def update_price(self, listing) -> None:
        """
        Обновляет цену объявления через REST API.

        Endpoint: POST /core/v1/items/{item_id}/update_price
        Единственная write-операция, доступная через REST (без фида).
        """
        self._request(
            'post',
            f'/core/v1/items/{listing.external_id}/update_price',
            'price',
            json={'price': int(listing.price_on_listing)},
        )

    def get_status(self, listing) -> dict:
        """
        Запрашивает текущий статус объявления у Avito.

        Endpoint: GET /core/v1/accounts/{user_id}/items/{item_id}/
        Trailing slash обязателен согласно официальной спецификации.
        """
        resp = self._request(
            'get',
            f'/core/v1/accounts/{self.account.external_id}/items/{listing.external_id}/',
            'update',
        )
        return resp.json()

    def get_feed_results(self, ad_ids: list[str]) -> list[dict]:
        """
        Возвращает соответствие наших ad_id → avito_id после обработки фида.

        Endpoint: GET /autoload/v2/items/avito_ids?query=ad_id1,ad_id2,...
        ad_id — это publish_idempotency_key листинга (использовался как <Id> в XML).
        Ответ: [{"ad_id": str, "avito_id": int | None}]
        """
        if not ad_ids:
            return []
        token = self._auth.get_token(self.account)
        resp = requests.get(
            f'{AVITO_API_BASE}/autoload/v2/items/avito_ids',
            headers={'Authorization': f'Bearer {token}'},
            params={'query': ','.join(ad_ids)},
            timeout=30,
        )
        if resp.status_code == 401:
            self._auth.invalidate(self.account)
            token = self._auth.get_token(self.account)
            resp = requests.get(
                f'{AVITO_API_BASE}/autoload/v2/items/avito_ids',
                headers={'Authorization': f'Bearer {token}'},
                params={'query': ','.join(ad_ids)},
                timeout=30,
            )
        resp.raise_for_status()
        return resp.json().get('items', [])

    def get_stats(
        self,
        item_ids: list[str],
        date_from: datetime.date,
        date_to: datetime.date,
    ) -> list[dict]:
        """
        Запрашивает статистику листингов из Avito Stats API.

        Разбивает item_ids на чанки по 200 (лимит Avito).
        История хранится 270 дней. periodGrouping="day" — обязательно для дневной разбивки.
        Возвращает список: [{itemId, stats: [{date, uniqViews, views, uniqContacts, ...}]}].
        """
        if not item_ids:
            return []

        token = self._auth.get_token(self.account)
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        url = f'{AVITO_API_BASE}/stats/v1/accounts/{self.account.external_id}/items'

        result = []
        for i in range(0, len(item_ids), _STATS_CHUNK):
            chunk = item_ids[i:i + _STATS_CHUNK]
            payload = {
                'dateFrom': date_from.isoformat(),
                'dateTo': date_to.isoformat(),
                'itemIds': [int(x) for x in chunk],
                'fields': ['uniqViews', 'views', 'uniqContacts', 'contacts'],
                'periodGrouping': 'day',
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            if resp.status_code == 401:
                self._auth.invalidate(self.account)
                token = self._auth.get_token(self.account)
                headers['Authorization'] = f'Bearer {token}'
                resp = requests.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            result.extend(resp.json().get('result', {}).get('items', []))

        return result

    # ------------------------------------------------------------------ #
    #  Настройка профиля Autoload (вызывается при онбординге)             #
    # ------------------------------------------------------------------ #

    def setup_autoload_profile(self, report_email: str) -> None:
        """
        Создаёт или обновляет профиль Avito Autoload для аккаунта.

        Устанавливает feed_url = публичный URL нашего S3-файла.
        Вызывается один раз при подключении аккаунта (онбординг).
        """
        token = self._auth.get_token(self.account)
        payload = {
            'agreement': True,
            'autoload_enabled': True,
            'report_email': report_email,
            'feeds_data': [
                {
                    'feed_name': f'MAP feed — {self.account.name}',
                    'feed_url': self._feed_public_url(),
                }
            ],
            # Каждый день в 3:00 и 12:00 МСК, до 50 000 объявлений за период
            'schedule': [
                {'rate': 50000, 'weekdays': [0, 1, 2, 3, 4, 5, 6], 'time_slots': [3, 12]},
            ],
        }
        resp = requests.post(
            f'{AVITO_API_BASE}/autoload/v2/profile',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json=payload,
            timeout=30,
        )
        if not resp.ok:
            logger.error(
                'Не удалось настроить Autoload профиль для account=%s: %s %s',
                self.account.pk, resp.status_code, resp.text[:200],
            )
            resp.raise_for_status()
