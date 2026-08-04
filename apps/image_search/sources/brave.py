"""Источник изображений: Brave Search API (Tier 3, платный).

Официальный REST API — не подвержен IP-банам как DDG.
Документация: https://api.search.brave.com/app/documentation/image-search/query
Квота: 1000 запросов/месяц бесплатно, затем $5/мес. Soft cap = 800.
"""

import logging

import requests
from django.conf import settings

from apps.image_search.models import BraveQuota
from apps.image_search.sources.base import BaseImageSource, ImageCandidate
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
        if not settings.BRAVE_SEARCH_API_KEY:
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
        api_key = settings.BRAVE_SEARCH_API_KEY
        if not api_key:
            logger.warning('[brave] BRAVE_SEARCH_API_KEY не задан')
            return []

        queries = self.build_queries()[:self.max_queries]
        candidates: list[ImageCandidate] = []
        seen_urls: set[str] = set()

        for query, confidence in queries:
            results = self._fetch(api_key, query)
            for rank, r in enumerate(results, start=1):
                url = r.get('properties', {}).get('url', '')
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                thumb = r.get('thumbnail', {})
                props = r.get('properties', {})
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
            resp = requests.get(
                _API_URL,
                headers={
                    'Accept': 'application/json',
                    'Accept-Encoding': 'gzip',
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
            )

            if resp.status_code == 401:
                logger.error('[brave] неверный API ключ (401)')
                return []
            if resp.status_code == 429:
                logger.error(
                    '[brave] ЛИМИТ ЗАПРОСОВ ИСЧЕРПАН (429) — пополните баланс на api.search.brave.com',
                )
                return []

            resp.raise_for_status()

            # Инкрементируем персистентный счётчик только при успешном ответе
            self._track_quota(resp)

            return resp.json().get('results', [])
        except Exception as exc:
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
