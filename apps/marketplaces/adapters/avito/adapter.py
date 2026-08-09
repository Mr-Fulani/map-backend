import datetime
import html
import logging
import re

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

from apps.core.http_responses import bounded_http_request
from apps.marketplaces.adapters.avito.auth import AvitoAuthManager
from apps.marketplaces.adapters.avito.error_handler import (
    ForbiddenError,
    NotFoundError,
    handle_avito_error,
)
from apps.marketplaces.adapters.avito.feed_builder import build_feed, build_stop_feed
from apps.marketplaces.adapters.avito.rate_limiter import AvitoRateLimiter, RateLimitError
from apps.marketplaces.base import BaseMarketplaceAdapter

logger = logging.getLogger(__name__)

AVITO_API_BASE = 'https://api.avito.ru'
_STATS_CHUNK = 200  # максимум item_ids за один запрос к Stats API


def _avito_request(requester, *args, **kwargs):
    return bounded_http_request(
        requester,
        *args,
        max_bytes=settings.AVITO_API_RESPONSE_MAX_BYTES,
        **kwargs,
    )


def _strip_html(text: str) -> str:
    """Убирает HTML-теги и спецсимволы из текста сообщения отчёта Avito."""
    no_tags = re.sub(r'<[^>]+>', ' ', text or '')
    return re.sub(r'\s+', ' ', html.unescape(no_tags)).strip()


def _format_avito_message(message: dict) -> str:
    """Превращает сообщение отчёта Autoload в одну читаемую строку без HTML."""
    title_value = message.get('title')
    description_value = message.get('description')
    if title_value is not None and not isinstance(title_value, str):
        return ''
    if description_value is not None and not isinstance(description_value, str):
        return ''
    title = (title_value or '').strip()
    description = _strip_html(description_value or '')
    text = f'{title} {description}'.strip()
    return f'• {text}' if text else ''


def _json_object(value, context: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f'Avito {context}: top-level JSON must be an object')
    return value


def _json_list(value, context: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f'Avito {context}: expected a list')
    return value


class FeedUploadError(Exception):
    """Не удалось загрузить фид на S3 или уведомить Avito."""


def _key_part(value: object, fallback: str) -> str:
    raw = str(value or '').strip().lower()
    raw = re.sub(r'\s+', '-', raw)
    raw = re.sub(r'[^0-9a-z_-]+', '', raw)
    raw = raw.strip('-_')
    return raw or fallback


class AvitoAdapter(BaseMarketplaceAdapter):
    """
    Адаптер Avito. Feed-based: publish/update/unpublish/delete работают через Autoload XML.
    Операции с ценой и статусом — через REST API.

    Публикация объявлений на Avito:
      1. Генерируем XML-фид (Avito Autoload formatVersion=3).
      2. Загружаем на Yandex S3 по пути feeds/{tenant_slug}/avito/{account_slug}/feed.xml.
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
        requester = getattr(requests, method)
        resp = _avito_request(
            requester, url, headers=self._headers(), timeout=30, **kwargs,
        )
        self._rl.handle_response_headers(dict(resp.headers), self.account)
        if resp.status_code == 401:
            self._auth.invalidate(self.account)
            resp = _avito_request(
                requester, url, headers=self._headers(), timeout=30, **kwargs,
            )
        handle_avito_error(resp)
        return resp

    def _feed_s3_key(self) -> str:
        tenant_slug = _key_part(getattr(self.account.tenant, 'slug', ''), 'tenant')
        marketplace = _key_part(getattr(self.account, 'marketplace', ''), 'marketplace')
        account_slug = _key_part(getattr(self.account, 'name', ''), f'account-{self.account.pk}')
        feed_key = f'feeds/{tenant_slug}/{marketplace}/{account_slug}-{self.account.pk}/feed.xml'
        prefix = str(getattr(settings, 'MEDIA_KEY_PREFIX', '') or '').strip('/')
        return f'{prefix}/{feed_key}' if prefix else feed_key

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
        resp = _avito_request(
            requests.post,
            f'{AVITO_API_BASE}/autoload/v1/upload',
            headers={'Authorization': f'Bearer {token}'},
            timeout=30,
        )
        if resp.status_code in (200, 204):
            return
        logger.warning(
            'autoload/v1/upload вернул %s для account=%s: %s',
            resp.status_code, self.account.pk, resp.text[:200],
        )
        # 429 — лимит «1 автозагрузка в час». Это не ошибка конфигурации,
        # а частотный лимит: пробрасываем RateLimitError, чтобы задача
        # повторила попытку после открытия окна.
        if resp.status_code == 429:
            raise RateLimitError(
                'Avito: автозагрузку можно запускать не чаще 1 раза в час. '
                'Повтор произойдёт автоматически через ~10 минут.'
            )
        raise FeedUploadError(
            f'Avito Autoload не принял фид: HTTP {resp.status_code}.'
        )

    def is_autoload_active(self) -> bool:
        """Проверяет, доступен ли профиль Avito Autoload для аккаунта."""
        try:
            profile = self.get_autoload_profile()
        except (ForbiddenError, NotFoundError):
            return False
        return bool(profile.get('autoload_enabled'))

    def get_autoload_profile(self) -> dict:
        """Возвращает настройки профиля Автозагрузки Avito."""
        resp = self._request('get', '/autoload/v2/profile', operation='status')
        return resp.json()

    def get_tariff_info(self) -> dict:
        """Возвращает текущий и запланированный тариф категории «Транспорт»."""
        resp = self._request('get', '/tariff/info/1', operation='status')
        return resp.json()

    def get_category_tree(self) -> list:
        """
        Возвращает дерево категорий Avito (GET /autoload/v1/user-docs/tree).

        Каждый узел: {name, slug, nested}. Поля доступны только у листовых узлов
        (см. get_node_fields). Используется командой sync_avito_categories.
        """
        token = self._auth.get_token(self.account)
        resp = _avito_request(
            requests.get,
            f'{AVITO_API_BASE}/autoload/v1/user-docs/tree',
            headers={'Authorization': f'Bearer {token}'},
            timeout=60,
        )
        resp.raise_for_status()
        payload = _json_object(resp.json(), 'category tree')
        categories = _json_list(payload.get('categories'), 'category tree categories')
        if not all(isinstance(item, dict) for item in categories):
            raise ValueError('Avito category tree: category items must be objects')
        return categories

    def get_node_fields(self, node_slug: str) -> dict:
        """
        Возвращает поля листовой категории Avito.

        GET /autoload/v1/user-docs/node/{node_slug}/fields. Для нелистового узла
        Avito отвечает 400 («not a leaf node»). В ответе fields[].content[] —
        правила заполнения: required, field_type, values, dependencies.
        """
        token = self._auth.get_token(self.account)
        resp = _avito_request(
            requests.get,
            f'{AVITO_API_BASE}/autoload/v1/user-docs/node/{node_slug}/fields',
            headers={'Authorization': f'Bearer {token}'},
            timeout=60,
        )
        resp.raise_for_status()
        return _json_object(resp.json(), 'node fields')

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

    def flush_stop(self) -> bool:
        """
        Загружает спец-фид STOP — снимает ВСЕ объявления аккаунта с публикации.

        Используется, когда активных объявлений не осталось (пустой фид Avito
        снятием не считает, нужна команда STOP).
        """
        self._upload_to_s3(build_stop_feed())
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
        resp = _avito_request(
            requests.get,
            f'{AVITO_API_BASE}/autoload/v2/items/avito_ids',
            headers={'Authorization': f'Bearer {token}'},
            params={'query': ','.join(ad_ids)},
            timeout=30,
        )
        if resp.status_code == 401:
            self._auth.invalidate(self.account)
            token = self._auth.get_token(self.account)
            resp = _avito_request(
                requests.get,
                f'{AVITO_API_BASE}/autoload/v2/items/avito_ids',
                headers={'Authorization': f'Bearer {token}'},
                params={'query': ','.join(ad_ids)},
                timeout=30,
            )
        if resp.status_code in (403, 404):
            raise FeedUploadError(
                'Автозагрузка Avito не подключена или профиль Autoload недоступен. '
                'Подключите Автозагрузку в настройках Avito и повторите публикацию.'
            )
        handle_avito_error(resp)
        return resp.json().get('items', [])

    def get_latest_upload(self) -> dict:
        """
        Возвращает последнюю загрузку Autoload (v4): {upload_id, status, stats, ...} или {}.

        status: 'processing' (Avito ещё обрабатывает фид), 'success', 'success_warning'.
        Нужно, чтобы не отклонять объявления, пока загрузка не завершена.
        Сетевые сбои не пробрасывает — возвращает {}.
        """
        token = self._auth.get_token(self.account)
        try:
            resp = _avito_request(
                requests.get,
                f'{AVITO_API_BASE}/autoload/v4/uploads',
                headers={'Authorization': f'Bearer {token}'},
                params={'per_page': 1, 'page': 1}, timeout=30,
            )
            if not resp.ok:
                return {}
            uploads = resp.json().get('uploads', [])
        except (requests.RequestException, ValueError, KeyError):
            return {}
        # Список приходит свежими сверху, но не полагаемся на это — берём по времени.
        return max(uploads, key=lambda u: u.get('started_at', ''), default={}) if uploads else {}

    def get_feed_item_errors(self, ad_ids: list[str]) -> dict[str, str]:
        """
        Возвращает {ad_id: человекочитаемый текст ошибок} из последней успешной загрузки.

        Источник — v4 GET /autoload/v4/uploads/last_successful/items (старый
        /autoload/v2/reports/{id}/items устарел и больше не отдаёт позиции).
        Собирает сообщения Avito с типом error/alarm по каждому ad_id. Сетевые
        сбои не пробрасывает — возвращает {}, чтобы вызывающий код мог откатиться
        на общий текст ошибки.
        """
        if not ad_ids:
            return {}
        token = self._auth.get_token(self.account)
        headers = {'Authorization': f'Bearer {token}'}
        wanted = set(ad_ids)
        result: dict[str, str] = {}
        max_pages = min(100, max(1, int(settings.AVITO_API_MAX_PAGES)))
        try:
            page = 1
            while page <= max_pages:
                resp = _avito_request(
                    requests.get,
                    f'{AVITO_API_BASE}/autoload/v4/uploads/last_successful/items',
                    headers=headers, params={'per_page': 100, 'page': page}, timeout=30,
                )
                if not resp.ok:
                    break
                body = _json_object(resp.json(), 'feed item errors')
                items = _json_list(body.get('items'), 'feed item errors items')
                for item in items[:100]:
                    if not isinstance(item, dict):
                        logger.warning('Avito feed item errors contains a non-object item; stopping pagination.')
                        return result
                    ad_id = item.get('ad_id')
                    if ad_id not in wanted:
                        continue
                    item_messages = _json_list(
                        item.get('messages'), 'feed item error messages',
                    )
                    selected_messages = item_messages[:100]
                    if not all(isinstance(message, dict) for message in selected_messages):
                        logger.warning('Avito feed item errors contains a malformed message; item skipped.')
                        continue
                    # Реальной ошибкой считаем только type=error; warning/alarm
                    # (напр. авто-определение SparePartType) — не повод отклонять.
                    if not any(m.get('type') == 'error' for m in selected_messages):
                        continue
                    messages = [
                        _format_avito_message(m)
                        for m in selected_messages
                        if m.get('type') in ('error', 'alarm')
                    ]
                    messages = [message for message in messages if message]
                    if messages:
                        result[ad_id] = '\n'.join(messages)
                meta = body.get('meta')
                if meta is None:
                    meta = {}
                if not isinstance(meta, dict):
                    logger.warning('Avito feed item errors returned malformed pagination metadata.')
                    break
                total_pages = meta.get('pages', 1)
                if (
                    isinstance(total_pages, bool)
                    or not isinstance(total_pages, int)
                    or total_pages < 1
                ):
                    logger.warning('Avito feed item errors returned invalid page count.')
                    break
                if page >= total_pages:
                    break
                if page == max_pages:
                    logger.warning(
                        'Avito feed item errors pagination stopped at hard limit %d/%d.',
                        max_pages,
                        total_pages,
                    )
                    break
                page += 1
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return result
        return result

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
            resp = _avito_request(
                requests.post, url, headers=headers, json=payload, timeout=30,
            )
            if resp.status_code == 401:
                self._auth.invalidate(self.account)
                token = self._auth.get_token(self.account)
                headers['Authorization'] = f'Bearer {token}'
                resp = _avito_request(
                    requests.post, url, headers=headers, json=payload, timeout=30,
                )
            resp.raise_for_status()
            result.extend(resp.json().get('result', {}).get('items', []))

        return result

    # ------------------------------------------------------------------ #
    #  Настройка профиля Autoload (вызывается при онбординге)             #
    # ------------------------------------------------------------------ #

    def setup_autoload_profile(self, report_email: str) -> None:
        """
        Создаёт или обновляет профиль Avito Autoload для аккаунта.

        Добавляет feed_url нашего S3-файла, сохраняя чужие фиды, расписание
        и явное состояние выключенного профиля.
        Вызывается один раз при подключении аккаунта (онбординг).
        """
        own_feed = {
            'feed_name': f'MAP feed — {self.account.name}',
            'feed_url': self._feed_public_url(),
        }
        try:
            current = self.get_autoload_profile()
        except NotFoundError:
            current = {}

        # Существующие фиды и расписание принадлежат клиенту: добавляем только
        # фид MAP и не включаем отключённый профиль без явного действия.
        feeds = [
            feed for feed in (current.get('feeds_data') or [])
            if (
                isinstance(feed, dict)
                and feed.get('feed_url') != own_feed['feed_url']
            )
        ]
        feeds.append(own_feed)
        payload = {
            'agreement': True,
            'autoload_enabled': current.get('autoload_enabled', True),
            'report_email': current.get('report_email') or report_email,
            'feeds_data': feeds,
            'schedule': current.get('schedule') or [
                {'rate': 50000, 'weekdays': [0, 1, 2, 3, 4, 5, 6], 'time_slots': [3, 12]},
            ],
        }
        self._request(
            'post',
            '/autoload/v2/profile',
            operation='status',
            json=payload,
        )
