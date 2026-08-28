import datetime
from copy import deepcopy
from dataclasses import dataclass
import html
import hmac
import logging
import re
import time

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

from apps.core.http_responses import bounded_http_request
from apps.core.telemetry import metric_count, metric_distribution
from apps.marketplaces.adapters.avito.auth import AvitoAuthManager
from apps.marketplaces.adapters.avito.error_handler import (
    ForbiddenError,
    NotFoundError,
    handle_avito_error,
)
from apps.marketplaces.adapters.avito.feed_builder import build_feed, build_stop_feed
from apps.marketplaces.adapters.avito.rate_limiter import (
    AUTOLOAD_RATE_LIMIT_RETRY_AFTER,
    AvitoRateLimiter,
    RateLimitError,
)
from apps.marketplaces.base import BaseMarketplaceAdapter

logger = logging.getLogger(__name__)

AVITO_API_BASE = 'https://api.avito.ru'
_STATS_CHUNK = 200  # максимум item_ids за один запрос к Stats API
_STATS_ITEM_ID_RE = re.compile(r'^[1-9][0-9]{0,19}$')
_FEED_ITEM_ERROR_PAGE_SIZE = 100
_FEED_ITEM_ERROR_MESSAGE_LIMIT = 100
_FEED_ITEM_ERROR_TEXT_LIMIT = 2000
_FEED_ITEM_AD_ID_LIMIT = 100


@dataclass(frozen=True)
class FeedItemErrorPage:
    """One bounded page of blocking Autoload item errors."""

    errors: dict[str, str]
    next_page: int | None

    @property
    def terminal(self) -> bool:
        return self.next_page is None


@dataclass(frozen=True)
class FeedItemOutcomePage:
    """One bounded page of outcomes from the current Autoload upload."""

    errors: dict[str, str]
    external_ids: dict[str, str]
    next_page: int | None

    @property
    def terminal(self) -> bool:
        return self.next_page is None


def normalize_avito_stats_item_id(value: object) -> str | None:
    """Return one exact positive decimal Avito item id, or skip it safely."""

    if isinstance(value, bool):
        return None
    candidate = str(value) if isinstance(value, (int, str)) else ''
    if _STATS_ITEM_ID_RE.fullmatch(candidate) is None:
        return None
    return candidate


def _avito_request(requester, *args, operation: str = 'other', **kwargs):
    """Execute one physical Avito HTTP request and emit bounded telemetry."""
    started_at = time.monotonic()
    outcome = 'failure'
    response_class = 'network_error'
    try:
        response = bounded_http_request(
            requester,
            *args,
            max_bytes=settings.AVITO_API_RESPONSE_MAX_BYTES,
            **kwargs,
        )
        if response.status_code == 429:
            metric_count(
                'map.provider.rate_limit',
                attributes={
                    'provider': 'avito',
                    'operation': operation,
                    'rate_limit_source': 'remote',
                },
            )
        response_class = f'{response.status_code // 100}xx'
        outcome = 'success' if 200 <= response.status_code < 300 else 'failure'
        return response
    finally:
        attrs = {
            'provider': 'avito',
            'operation': operation,
            'outcome': outcome,
            'response_class': response_class,
        }
        metric_count('map.provider.request', attributes=attrs)
        metric_distribution(
            'map.provider.request.duration',
            time.monotonic() - started_at,
            unit='second',
            attributes=attrs,
        )


def _strip_html(text: str) -> str:
    """Убирает HTML-теги и спецсимволы из текста сообщения отчёта Avito."""
    decoded = html.unescape(text or '')
    no_tags = re.sub(r'<[^>]+>', ' ', decoded)
    no_controls = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', no_tags)
    return re.sub(r'\s+', ' ', no_controls).strip()


def _format_avito_message(message: dict) -> str:
    """Превращает сообщение отчёта Autoload в одну читаемую строку без HTML."""
    title_value = message.get('title')
    description_value = message.get('description')
    if title_value is not None and not isinstance(title_value, str):
        return ''
    if description_value is not None and not isinstance(description_value, str):
        return ''
    title = _strip_html(title_value or '')
    description = _strip_html(description_value or '')
    text = f'{title} {description}'.strip()
    return f'• {text}' if text else ''


def _bounded_feed_item_error(messages: list[str]) -> str:
    """Join sanitized provider messages without growing task/DB payloads."""

    return '\n'.join(messages)[:_FEED_ITEM_ERROR_TEXT_LIMIT].rstrip()


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


class FeedEndpointIdentityHold(FeedUploadError):
    """The feed locator is fenced pending an explicit identity review."""


class AmbiguousFeedSubmissionError(FeedUploadError):
    """Avito may have accepted the non-idempotent Autoload POST."""


_AUTOLOAD_SAFE_REJECTION_STATUSES = frozenset({
    400,
    401,
    403,
    404,
    405,
    409,
    410,
    413,
    415,
    422,
})


@dataclass(frozen=True)
class FeedStorageLocator:
    """One immutable key/URL pair used by a physical upload."""

    object_key: str
    public_url: str


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
            requester,
            url,
            operation=operation,
            headers=self._headers(),
            timeout=30,
            **kwargs,
        )
        self._rl.handle_response_headers(dict(resp.headers), self.account)
        if resp.status_code == 401:
            self._auth.invalidate(self.account)
            resp = _avito_request(
                requester,
                url,
                operation=operation,
                headers=self._headers(),
                timeout=30,
                **kwargs,
            )
        handle_avito_error(resp)
        return resp

    def _legacy_feed_s3_key(self) -> str:
        tenant_slug = _key_part(getattr(self.account.tenant, 'slug', ''), 'tenant')
        marketplace = _key_part(getattr(self.account, 'marketplace', ''), 'marketplace')
        account_slug = _key_part(getattr(self.account, 'name', ''), f'account-{self.account.pk}')
        feed_key = f'feeds/{tenant_slug}/{marketplace}/{account_slug}-{self.account.pk}/feed.xml'
        prefix = str(getattr(settings, 'MEDIA_KEY_PREFIX', '') or '').strip('/')
        return f'{prefix}/{feed_key}' if prefix else feed_key

    def _legacy_feed_locator(self) -> FeedStorageLocator:
        from apps.marketplaces.feed_endpoint import (
            FeedEndpointConfigurationError,
            canonical_marketplace_feed_cdn_origin,
        )

        key = self._legacy_feed_s3_key()
        bucket = str(getattr(settings, 'YC_S3_BUCKET', '') or '').strip()
        if (
            not bucket
            or any(character.isspace() for character in bucket)
            or any(character in bucket for character in '/?#\\')
        ):
            raise FeedUploadError('S3 feed bucket is not safely configured.')
        try:
            cdn_origin = canonical_marketplace_feed_cdn_origin(
                getattr(settings, 'YC_CDN_DOMAIN', ''),
            )
        except FeedEndpointConfigurationError as exc:
            raise FeedUploadError(
                'S3 feed CDN authority is not safely configured.',
            ) from exc
        public_url = (
            f'{cdn_origin}/{key}'
            if cdn_origin
            else f'https://storage.yandexcloud.net/{bucket}/{key}'
        )
        return FeedStorageLocator(object_key=key, public_url=public_url)

    def _load_stable_feed_endpoint(self):
        """Read and identity-check one endpoint generation, when provisioned."""

        from apps.marketplaces.feed_workflow import account_identity_digest
        from apps.marketplaces.models import MarketplaceFeedEndpoint

        endpoint = (
            MarketplaceFeedEndpoint.objects.select_related(
                'account', 'account__tenant',
            )
            .filter(account_id=self.account.pk)
            .first()
        )
        if endpoint is None:
            return None

        stored_digest = str(endpoint.owner_identity_digest or '')
        current_digest = account_identity_digest(endpoint.account)
        adapter_digest = account_identity_digest(self.account)
        current_owner_matches = hmac.compare_digest(stored_digest, current_digest)
        adapter_owner_matches = hmac.compare_digest(stored_digest, adapter_digest)
        owner_is_live = (
            endpoint.account.deleted_at is None
            and endpoint.account.is_active
            and endpoint.account.tenant.is_active
        )
        if not (
            current_owner_matches
            & adapter_owner_matches
            & owner_is_live
            & (
                endpoint.storage_mode
                == MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE
            )
        ):
            raise FeedEndpointIdentityHold(
                'Feed endpoint identity requires manual review.',
            )
        return endpoint

    def _stable_feed_locator(
        self,
        *,
        require_serve_enabled: bool = False,
    ) -> FeedStorageLocator | None:
        """Resolve a physical object locator under the writer lifecycle."""

        from apps.marketplaces.feed_endpoint import legacy_bridge_target_url
        from apps.marketplaces.models import MarketplaceFeedEndpoint

        endpoint = self._load_stable_feed_endpoint()
        if endpoint is None:
            return None
        servable_states = {
            MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
            MarketplaceFeedEndpoint.ProfileState.MIGRATING,
            MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
            MarketplaceFeedEndpoint.ProfileState.VERIFIED,
        }
        if (
            endpoint.profile_state not in servable_states
            or (require_serve_enabled and not endpoint.serve_enabled)
        ):
            raise FeedEndpointIdentityHold(
                'Feed endpoint lifecycle requires manual review.',
            )
        target = legacy_bridge_target_url(endpoint)
        if target is None:
            raise FeedEndpointIdentityHold(
                'Feed endpoint locator requires manual review.',
            )
        return FeedStorageLocator(
            object_key=endpoint.legacy_object_key,
            public_url=target,
        )

    def _provider_profile_feed_url(self) -> str:
        """Keep normal onboarding outside the resumable migration writer."""

        endpoint = self._load_stable_feed_endpoint()
        if endpoint is None:
            return self._legacy_feed_locator().public_url
        raise FeedEndpointIdentityHold(
            'Stable feed profile is owned by the migration workflow.',
        )

    def _resolve_feed_locator(self) -> FeedStorageLocator:
        """Use one frozen endpoint locator, or the pre-rollout legacy fallback."""

        return self._stable_feed_locator() or self._legacy_feed_locator()

    def _assert_autoload_feed_route_servable(self) -> None:
        """Fence the stable URL before the non-idempotent provider trigger."""

        from apps.marketplaces.feed_cutover import private_feed_cutover_enabled
        from apps.marketplaces.feed_workflow import account_identity_digest
        from apps.marketplaces.models import MarketplaceFeedEndpoint

        if not private_feed_cutover_enabled(self.account.pk):
            self._stable_feed_locator(require_serve_enabled=True)
            return
        endpoint = (
            MarketplaceFeedEndpoint.objects.select_related(
                'account', 'account__tenant',
            )
            .filter(account_id=self.account.pk)
            .first()
        )
        if endpoint is None:
            raise FeedEndpointIdentityHold(
                'Private feed endpoint is unavailable.',
            )
        stored_digest = str(endpoint.owner_identity_digest or '')
        live_digest = account_identity_digest(endpoint.account)
        adapter_digest = account_identity_digest(self.account)
        if not (
            endpoint.storage_mode
            == MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION
            and endpoint.serve_enabled is True
            and endpoint.current_artifact_id is not None
            and endpoint.profile_state
            == MarketplaceFeedEndpoint.ProfileState.VERIFIED
            and endpoint.source_intent_revision
            == endpoint.account.feed_intent_revision
            and endpoint.account.deleted_at is None
            and endpoint.account.is_active is True
            and endpoint.account.tenant.is_active is True
            and hmac.compare_digest(stored_digest, live_digest)
            and hmac.compare_digest(stored_digest, adapter_digest)
        ):
            raise FeedEndpointIdentityHold(
                'Private feed endpoint is not safely servable.',
            )

    def _feed_s3_key(self) -> str:
        locator = self._stable_feed_locator()
        return locator.object_key if locator is not None else self._legacy_feed_s3_key()

    def _feed_public_url(self) -> str:
        return self._provider_profile_feed_url()

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
        locator = self._resolve_feed_locator()
        try:
            self._s3_client().put_object(
                Bucket=settings.YC_S3_BUCKET,
                Key=locator.object_key,
                Body=feed_bytes,
                ContentType='application/xml',
                ACL='public-read',
            )
        except (BotoCoreError, ClientError) as exc:
            raise FeedUploadError(f'Ошибка загрузки фида на S3: {exc}') from exc
        return locator.public_url

    def _trigger_autoload(self) -> None:
        """Уведомляет Avito о новом фиде через POST /autoload/v1/upload."""
        # Re-read the endpoint after upload. A credential rotation between
        # S3 PUT and this non-idempotent POST must stop at the provider boundary.
        self._assert_autoload_feed_route_servable()
        token = self._auth.get_token(self.account)
        resp = _avito_request(
            requests.post,
            f'{AVITO_API_BASE}/autoload/v1/upload',
            operation='autoload',
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
            raise RateLimitError(retry_after=AUTOLOAD_RATE_LIMIT_RETRY_AFTER)
        if resp.status_code in _AUTOLOAD_SAFE_REJECTION_STATUSES:
            raise FeedUploadError(
                f'Avito Autoload не принял фид: HTTP {resp.status_code}.'
            )
        raise AmbiguousFeedSubmissionError(
            'Avito Autoload вернул неоднозначный ответ: '
            f'HTTP {resp.status_code}; требуется сверка запуска.'
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
            operation='status',
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
            operation='status',
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
            operation='feed_poll',
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
                operation='feed_poll',
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

    def get_latest_upload(self, *, strict: bool = False) -> dict:
        """
        Возвращает последнюю загрузку Autoload (v4): {upload_id, status, stats, ...} или {}.

        status: 'processing' (Avito ещё обрабатывает фид), 'success', 'success_warning'.
        Legacy-вызовы превращают сетевые/JSON-сбои в ``{}``. В строгом режиме
        ошибки пробрасываются durable-оркестратору, чтобы outage нельзя было
        принять за доказанное отсутствие загрузки.
        """
        token = self._auth.get_token(self.account)
        try:
            resp = _avito_request(
                requests.get,
                f'{AVITO_API_BASE}/autoload/v4/uploads',
                operation='feed_poll',
                headers={'Authorization': f'Bearer {token}'},
                params={'per_page': 1, 'page': 1}, timeout=30,
            )
            if not resp.ok:
                if strict:
                    handle_avito_error(resp)
                return {}
            payload = resp.json()
            if strict:
                payload = _json_object(payload, 'latest upload response')
                uploads = _json_list(payload.get('uploads'), 'latest upload list')
                if any(not isinstance(upload, dict) for upload in uploads):
                    raise ValueError('Avito latest upload list must contain objects')
            else:
                uploads = payload.get('uploads', [])
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
            if strict:
                raise
            return {}
        # Список приходит свежими сверху, но не полагаемся на это — берём по времени.
        return max(uploads, key=lambda u: u.get('started_at', ''), default={}) if uploads else {}

    def get_feed_item_error_page(self, page: int) -> FeedItemErrorPage:
        """Return exactly one validated page of blocking feed item errors."""

        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 100:
            raise ValueError('Avito feed item errors page must be between 1 and 100')

        token = self._auth.get_token(self.account)
        resp = _avito_request(
            requests.get,
            f'{AVITO_API_BASE}/autoload/v4/uploads/last_successful/items',
            operation='feed_poll',
            headers={'Authorization': f'Bearer {token}'},
            params={'per_page': _FEED_ITEM_ERROR_PAGE_SIZE, 'page': page},
            timeout=30,
        )
        if resp.status_code == 401:
            self._auth.invalidate(self.account)
        if not resp.ok:
            handle_avito_error(resp)

        body = _json_object(resp.json(), 'feed item errors')
        items = _json_list(body.get('items'), 'feed item errors items')
        if len(items) > _FEED_ITEM_ERROR_PAGE_SIZE:
            raise ValueError('Avito feed item errors page exceeds 100 items')

        result: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('Avito feed item errors item must be an object')
            ad_id = item.get('ad_id')
            if not isinstance(ad_id, str) or not 0 < len(ad_id) <= _FEED_ITEM_AD_ID_LIMIT:
                continue
            item_messages = _json_list(
                item.get('messages'), 'feed item error messages',
            )
            if len(item_messages) > _FEED_ITEM_ERROR_MESSAGE_LIMIT:
                raise ValueError('Avito feed item error exceeds 100 messages')
            if not all(isinstance(message, dict) for message in item_messages):
                raise ValueError('Avito feed item error message must be an object')
            if not any(message.get('type') == 'error' for message in item_messages):
                continue
            messages = [
                _format_avito_message(message)
                for message in item_messages
                if message.get('type') in ('error', 'alarm')
            ]
            messages = [message for message in messages if message]
            if not messages:
                continue
            combined = _bounded_feed_item_error(messages)
            if ad_id in result:
                combined = _bounded_feed_item_error([result[ad_id], combined])
            result[ad_id] = combined

        meta = body.get('meta')
        if meta is None:
            meta = {}
        if not isinstance(meta, dict):
            raise ValueError('Avito feed item errors metadata must be an object')
        total_pages = meta.get('pages', 1)
        if (
            isinstance(total_pages, bool)
            or not isinstance(total_pages, int)
            or total_pages < 1
        ):
            raise ValueError('Avito feed item errors page count must be positive')
        reported_page = meta.get('page')
        if reported_page is not None and reported_page != page:
            raise ValueError('Avito feed item errors metadata page mismatch')
        per_page = meta.get('per_page')
        if per_page is not None and (
            isinstance(per_page, bool)
            or not isinstance(per_page, int)
            or not 1 <= per_page <= _FEED_ITEM_ERROR_PAGE_SIZE
        ):
            raise ValueError('Avito feed item errors per_page exceeds 100')

        next_page = page + 1 if page < total_pages else None
        return FeedItemErrorPage(errors=result, next_page=next_page)

    def get_current_feed_item_outcome_page(self, page: int) -> FeedItemOutcomePage:
        """Return one validated page of exact current-upload item outcomes."""

        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 100:
            raise ValueError('Avito current feed outcomes page must be between 1 and 100')

        token = self._auth.get_token(self.account)
        resp = _avito_request(
            requests.get,
            f'{AVITO_API_BASE}/autoload/v4/uploads/current/items',
            operation='feed_poll',
            headers={'Authorization': f'Bearer {token}'},
            params={'perPage': _FEED_ITEM_ERROR_PAGE_SIZE, 'page': page},
            timeout=30,
        )
        if resp.status_code == 401:
            self._auth.invalidate(self.account)
        if not resp.ok:
            handle_avito_error(resp)

        body = _json_object(resp.json(), 'current feed item outcomes')
        items = _json_list(body.get('items'), 'current feed item outcomes items')
        if len(items) > _FEED_ITEM_ERROR_PAGE_SIZE:
            raise ValueError('Avito current feed outcomes page exceeds 100 items')

        errors: dict[str, str] = {}
        external_ids: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('Avito current feed outcome item must be an object')
            ad_id = item.get('ad_id')
            if not isinstance(ad_id, str) or not 0 < len(ad_id) <= _FEED_ITEM_AD_ID_LIMIT:
                continue
            item_messages = _json_list(
                item.get('messages'), 'current feed item outcome messages',
            )
            if len(item_messages) > _FEED_ITEM_ERROR_MESSAGE_LIMIT:
                raise ValueError('Avito current feed outcome exceeds 100 messages')
            if not all(isinstance(message, dict) for message in item_messages):
                raise ValueError('Avito current feed outcome message must be an object')

            if any(message.get('type') == 'error' for message in item_messages):
                messages = [
                    _format_avito_message(message)
                    for message in item_messages
                    if message.get('type') in ('error', 'alarm')
                ]
                messages = [message for message in messages if message]
                if messages:
                    combined = _bounded_feed_item_error(messages)
                    if ad_id in errors:
                        combined = _bounded_feed_item_error([errors[ad_id], combined])
                    errors[ad_id] = combined
                    external_ids.pop(ad_id, None)
                continue

            if str(item.get('avito_status') or '').strip().casefold() != 'active':
                continue
            external_id = normalize_avito_stats_item_id(item.get('avito_id'))
            if external_id is None or ad_id in errors:
                continue
            previous = external_ids.get(ad_id)
            if previous is not None and previous != external_id:
                raise ValueError(
                    'Avito current feed outcomes contain conflicting item ids',
                )
            external_ids[ad_id] = external_id

        meta = body.get('meta')
        if meta is None:
            meta = {}
        if not isinstance(meta, dict):
            raise ValueError('Avito current feed outcomes metadata must be an object')
        total_pages = meta.get('pages', 1)
        if (
            isinstance(total_pages, bool)
            or not isinstance(total_pages, int)
            or total_pages < 1
        ):
            raise ValueError('Avito current feed outcomes page count must be positive')
        reported_page = meta.get('page')
        if reported_page is not None and reported_page != page:
            raise ValueError('Avito current feed outcomes metadata page mismatch')
        per_page = meta.get('perPage', meta.get('per_page'))
        if per_page is not None and (
            isinstance(per_page, bool)
            or not isinstance(per_page, int)
            or not 1 <= per_page <= _FEED_ITEM_ERROR_PAGE_SIZE
        ):
            raise ValueError('Avito current feed outcomes perPage exceeds 100')

        next_page = page + 1 if page < total_pages else None
        return FeedItemOutcomePage(
            errors=errors,
            external_ids=external_ids,
            next_page=next_page,
        )

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
        wanted = set(ad_ids)
        result: dict[str, str] = {}
        max_pages = min(100, max(1, int(settings.AVITO_API_MAX_PAGES)))
        page = 1
        try:
            while page <= max_pages:
                page_result = self.get_feed_item_error_page(page)
                result.update({
                    ad_id: reason
                    for ad_id, reason in page_result.errors.items()
                    if ad_id in wanted
                })
                if page_result.terminal:
                    break
                if page == max_pages:
                    logger.warning(
                        'Avito feed item errors pagination stopped at hard limit %d.',
                        max_pages,
                    )
                    break
                next_page = page_result.next_page
                if next_page is None:
                    break
                page = next_page
        except Exception:
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
        normalized_item_ids = [
            int(candidate)
            for value in item_ids
            if (candidate := normalize_avito_stats_item_id(value)) is not None
        ]
        if not normalized_item_ids:
            return []

        token = self._auth.get_token(self.account)
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        url = f'{AVITO_API_BASE}/stats/v1/accounts/{self.account.external_id}/items'

        result = []
        for i in range(0, len(normalized_item_ids), _STATS_CHUNK):
            chunk = normalized_item_ids[i:i + _STATS_CHUNK]
            payload = {
                'dateFrom': date_from.isoformat(),
                'dateTo': date_to.isoformat(),
                'itemIds': chunk,
                'fields': ['uniqViews', 'views', 'uniqContacts', 'contacts'],
                'periodGrouping': 'day',
            }
            resp = _avito_request(
                requests.post,
                url,
                operation='stats',
                headers=headers,
                json=payload,
                timeout=30,
            )
            if resp.status_code == 401:
                self._auth.invalidate(self.account)
                token = self._auth.get_token(self.account)
                headers['Authorization'] = f'Bearer {token}'
                resp = _avito_request(
                    requests.post,
                    url,
                    operation='stats',
                    headers=headers,
                    json=payload,
                    timeout=30,
                )
            resp.raise_for_status()
            result.extend(resp.json().get('result', {}).get('items', []))

        return result

    # ------------------------------------------------------------------ #
    #  Настройка профиля Autoload (вызывается при онбординге)             #
    # ------------------------------------------------------------------ #

    def _build_autoload_profile_payload(
        self,
        current: dict,
        report_email: str,
        *,
        feed_url: str,
        replaced_feed_urls: tuple[str, ...] = (),
    ) -> dict:
        """Build the existing onboarding upsert while preserving user fields."""

        if not isinstance(current, dict):
            raise ValueError('Avito Autoload profile must be an object.')
        replaced_urls = {feed_url, *replaced_feed_urls}
        feeds = [
            deepcopy(feed)
            for feed in (current.get('feeds_data') or [])
            if (
                isinstance(feed, dict)
                and feed.get('feed_url') not in replaced_urls
            )
        ]
        feeds.append({
            'feed_name': f'MAP feed — {self.account.name}',
            'feed_url': feed_url,
        })
        payload = {
            'agreement': True,
            'autoload_enabled': current.get('autoload_enabled', True),
            'report_email': current.get('report_email') or report_email,
            'feeds_data': feeds,
            'schedule': deepcopy(current.get('schedule')) or [
                {
                    'rate': 50000,
                    'weekdays': [0, 1, 2, 3, 4, 5, 6],
                    'time_slots': [3, 12],
                },
            ],
        }
        for optional_key in ('allow_pay_over_limit', 'uploadMode'):
            if optional_key in current:
                payload[optional_key] = deepcopy(current[optional_key])
        return payload

    def setup_autoload_profile(
        self,
        report_email: str,
        *,
        feed_url: str | None = None,
        replaced_feed_urls: tuple[str, ...] = (),
    ) -> None:
        """
        Создаёт или обновляет профиль Avito Autoload для аккаунта.

        Добавляет feed_url нашего S3-файла, сохраняя чужие фиды, расписание
        и явное состояние выключенного профиля.
        Вызывается один раз при подключении аккаунта (онбординг).
        """
        target_feed_url = feed_url or self._feed_public_url()
        try:
            current = self.get_autoload_profile()
        except NotFoundError:
            current = {}

        payload = self._build_autoload_profile_payload(
            current,
            report_email,
            feed_url=target_feed_url,
            replaced_feed_urls=replaced_feed_urls,
        )
        self._request(
            'post',
            '/autoload/v2/profile',
            operation='status',
            json=payload,
        )
