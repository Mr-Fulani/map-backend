"""Источник изображений: Brave Search API (Tier 3, платный).

Официальный REST API — не подвержен IP-банам как DDG.
Документация: https://api.search.brave.com/app/documentation/image-search/query
"""

import logging

import requests
from django.conf import settings

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
    """

    source_id = 'brave'
    tier = 3
    is_free = False
    requires_key = True

    def is_available(self) -> bool:
        """True если BRAVE_SEARCH_API_KEY задан в настройках."""
        return bool(settings.BRAVE_SEARCH_API_KEY)

    def search(self) -> list[ImageCandidate]:
        """Ищет изображения через Brave Search API.

        Returns:
            Список ImageCandidate с width/height и confidence в raw_meta.
        """
        api_key = settings.BRAVE_SEARCH_API_KEY
        if not api_key:
            logger.warning('[brave] BRAVE_SEARCH_API_KEY не задан')
            return []

        queries = self.build_queries()[:_MAX_QUERIES]
        candidates: list[ImageCandidate] = []
        seen_urls: set[str] = set()

        for query, confidence in queries:
            results = self._fetch(api_key, query)
            for r in results:
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
                    },
                ))

        return candidates

    def _fetch(self, api_key: str, query: str) -> list[dict]:
        """Выполняет один запрос к Brave Image Search API."""
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
                logger.warning('[brave] превышен лимит запросов (429)')
                return []
            resp.raise_for_status()
            return resp.json().get('results', [])
        except Exception as exc:
            logger.warning('[brave] ошибка для %r: %s', query, exc)
            return []
