"""Источник изображений: Brave Search API (Tier 3, платный).

Официальный REST API — не подвержен IP-банам как DDG.
Документация: https://api.search.brave.com/app/documentation/image-search/query
Квота: 1000 запросов/месяц бесплатно, затем $5/мес. Soft cap = 800.
"""

import logging

import requests
from django.conf import settings

from apps.core.http_responses import bounded_http_request, trusted_api_max_bytes
from apps.image_search.models import BraveQuota
from apps.image_search.sources.base import BaseImageSource, ImageCandidate
from apps.image_search.sources.connection import image_source_api_key, image_source_connection
from apps.image_search.sources.registry import register

logger = logging.getLogger(__name__)

_API_URL = 'https://api.search.brave.com/res/v1/images/search'
_MAX_RESULTS_PER_QUERY = 15
_MAX_QUERIES = 2
_TIMEOUT_SEC = 10


@register
class BraveImageSource(BaseImageSource):
    """Источник изображений через Brave Search API.

    Tier 3 — надёжнее DDG, официальный API с ключом.
    Автоматически отключается при достижении soft cap (800 запросов/месяц).
    """

    source_id = 'brave'
    tier = 3
    is_free = False
    requires_key = True
    max_queries = _MAX_QUERIES

    def is_available(self) -> bool:
        """True если API-ключ задан и месячный soft cap (800) не достигнут."""
        tenant = getattr(self.product, 'tenant', None)
        connection = image_source_connection(self.source_id, tenant)
        if not connection.enabled:
            return False
        if not image_source_api_key(
            self.source_id, tenant, 'BRAVE_SEARCH_API_KEY',
            getattr(settings, 'BRAVE_SEARCH_API_KEY', ''),
        ):
            return False
        if BraveQuota.is_soft_cap_reached():
            logger.warning(
                '[brave] soft cap %d достигнут — источник отключён до конца месяца. '
                'Для продолжения пополните баланс на api.search.brave.com ($5) '
                'и обратитесь в поддержку dodugir.com.',
                BraveQuota.SOFT_CAP,
            )
            return False
        return True

    def build_queries(self) -> list[tuple[str, str]]:
        """Добавляет «автозапчасть» к запросам без категории — Brave без контекста нерелевантен."""
        base = super().build_queries()
        result = []
        for query, confidence in base:
            if 'запчасть' not in query.lower() and not getattr(self.product, 'category_1c', ''):
                query = f'{query} автозапчасть'
            query = f'{query} -logo -схема -manual'
            result.append((query, confidence))
        return result

    def search(self) -> list[ImageCandidate]:
        """Ищет изображения через Brave Search API.

        Returns:
            Список ImageCandidate с width/height и confidence в raw_meta.
        """
        api_key = image_source_api_key(
            self.source_id, getattr(self.product, 'tenant', None), 'BRAVE_SEARCH_API_KEY',
            getattr(settings, 'BRAVE_SEARCH_API_KEY', ''),
        )
        if not api_key:
            logger.warning('[brave] BRAVE_SEARCH_API_KEY не задан')
            return []

        queries = self.build_queries()[:self.max_queries]
        candidates: list[ImageCandidate] = []
        seen_urls: set[str] = set()

        for query, confidence in queries:
            results = self._fetch(api_key, query)
            for rank, r in enumerate(results, start=1):
                props = r.get('properties') or {}
                thumb = r.get('thumbnail') or {}
                url = props.get('url', '')
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                candidates.append(ImageCandidate(
                    url=url,
                    source_id=self.source_id,
                    tier=self.tier,
                    width=props.get('width', thumb.get('width', 0)),
                    height=props.get('height', thumb.get('height', 0)),
                    raw_meta={
                        'confidence': confidence,
                        'title': r.get('title', ''),
                        'query': query,
                        'rank': rank,
                    },
                ))

        return candidates

    def _fetch(self, api_key: str, query: str) -> list[dict]:
        """Выполняет один HTTP-запрос к Brave Image Search API."""
        try:
            resp = bounded_http_request(
                requests.get,
                _API_URL,
                headers={
                    'Accept': 'application/json',
                    'X-Subscription-Token': api_key,
                },
                params={
                    'q': query,
                    'count': _MAX_RESULTS_PER_QUERY,
                    'search_lang': 'ru',
                    'country': 'ru',
                    'safesearch': 'off',
                },
                timeout=_TIMEOUT_SEC,
                max_bytes=trusted_api_max_bytes(settings),
            )

            if resp.status_code == 401:
                self.last_error = 'Brave: неверный API-ключ.'
                self.last_error_code = 'authentication_error'
                logger.error('[brave] неверный API ключ (401)')
                return []
            if resp.status_code == 429:
                self.last_error = 'Brave временно ограничил запросы.'
                self.last_error_code = 'rate_limited'
                logger.error(
                    '[brave] ЛИМИТ ЗАПРОСОВ ИСЧЕРПАН (429) — пополните баланс на api.search.brave.com',
                )
                return []

            resp.raise_for_status()

            # Инкрементируем персистентный счётчик только при успешном ответе
            self._track_quota(resp)

            payload = resp.json()
            if not isinstance(payload, dict):
                raise ValueError('top-level JSON must be an object')
            results = payload.get('results')
            if results is None:
                return []
            if not isinstance(results, list):
                raise ValueError('results must be a list')

            selected = results[:_MAX_RESULTS_PER_QUERY]
            for item in selected:
                if not isinstance(item, dict):
                    raise ValueError('results items must be objects')
                properties = item.get('properties')
                thumbnail = item.get('thumbnail')
                if properties is not None and not isinstance(properties, dict):
                    raise ValueError('properties must be an object')
                if thumbnail is not None and not isinstance(thumbnail, dict):
                    raise ValueError('thumbnail must be an object')
                url = (properties or {}).get('url')
                if url is not None and not isinstance(url, str):
                    raise ValueError('properties.url must be a string')
                title = item.get('title')
                if title is not None and not isinstance(title, str):
                    raise ValueError('title must be a string')
                for dimensions in (properties or {}, thumbnail or {}):
                    for field_name in ('width', 'height'):
                        dimension = dimensions.get(field_name)
                        if (
                            dimension is not None
                            and (
                                isinstance(dimension, bool)
                                or not isinstance(dimension, int)
                                or dimension < 0
                            )
                        ):
                            raise ValueError(
                                f'{field_name} must be a non-negative integer',
                            )
            return selected
        except ValueError as exc:
            self.last_error = f'Brave вернул некорректный ответ: {exc}'
            self.last_error_code = 'invalid_response'
            logger.warning('[brave] некорректный ответ для %r: %s', query, exc)
            return []
        except Exception as exc:
            self.last_error = f'Brave недоступен: {exc}'
            self.last_error_code = 'source_error'
            logger.warning('[brave] ошибка для %r: %s', query, exc)
            return []

    @staticmethod
    def _track_quota(resp) -> None:
        """Атомарно инкрементирует DB-счётчик и логирует прогресс квоты.

        Лимит обновляется из заголовка X-RateLimit-Limit (Brave возвращает в каждом ответе).
        При достижении soft cap логирует критическую ошибку и ставит флаг cap_notified.
        """
        quota = BraveQuota.increment()

        # Обновляем лимит из заголовка если он пришёл и отличается
        limit_hdr = resp.headers.get('X-RateLimit-Limit')
        if limit_hdr:
            try:
                limit_val = int(limit_hdr)
                if quota.limit != limit_val:
                    BraveQuota.objects.filter(pk=quota.pk).update(limit=limit_val)
                    quota.limit = limit_val
            except (ValueError, TypeError):
                pass

        used = quota.requests_used

        if used >= BraveQuota.SOFT_CAP and not quota.cap_notified:
            BraveQuota.objects.filter(pk=quota.pk).update(cap_notified=True)
            logger.critical(
                '[brave] SOFT CAP ДОСТИГНУТ: %d/%d запросов в %s. '
                'Brave отключён до конца месяца. '
                'Пополните баланс на api.search.brave.com ($5) и обратитесь в поддержку dodugir.com.',
                used, quota.limit, quota.period,
            )
            return

        remaining_hdr = resp.headers.get('X-RateLimit-Remaining')
        if remaining_hdr is None:
            logger.info('[brave] %d запросов использовано в %s', used, quota.period)
            return

        try:
            remaining = int(remaining_hdr)
            if remaining <= 200:
                logger.error('[brave] квота заканчивается: осталось ~%d/%d', remaining, quota.limit)
            elif remaining <= 400:
                logger.warning('[brave] квота: осталось ~%d/%d', remaining, quota.limit)
            else:
                logger.info('[brave] %d исп., осталось ~%d/%d', used, remaining, quota.limit)
        except (ValueError, TypeError):
            pass
