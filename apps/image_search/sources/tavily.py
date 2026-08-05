"""Image candidates returned by Tavily Search API."""

import logging

import requests
from django.conf import settings
from django.utils.html import strip_tags

from apps.image_search.sources.base import BaseImageSource, ImageCandidate
from apps.image_search.sources.connection import image_source_api_key, image_source_connection
from apps.image_search.sources.registry import register

logger = logging.getLogger(__name__)

_API_URL = 'https://api.tavily.com/search'


@register
class TavilyImageSource(BaseImageSource):
    """Provider-neutral fallback using Tavily's ``include_images`` response."""

    source_id = 'tavily'
    tier = 3
    is_free = False
    requires_key = True
    max_queries = 2

    def is_available(self) -> bool:
        tenant = getattr(self.product, 'tenant', None)
        connection = image_source_connection(self.source_id, tenant)
        return connection.enabled and bool(
            image_source_api_key(
                self.source_id, tenant, 'TAVILY_API_KEY',
                getattr(settings, 'TAVILY_API_KEY', ''),
            )
        )

    def search(self) -> list[ImageCandidate]:
        tenant = getattr(self.product, 'tenant', None)
        connection = image_source_connection(self.source_id, tenant)
        api_key = image_source_api_key(
            self.source_id, tenant, 'TAVILY_API_KEY',
            getattr(settings, 'TAVILY_API_KEY', ''),
        )
        if not api_key:
            return []

        candidates: list[ImageCandidate] = []
        seen_urls: set[str] = set()
        for query, confidence in self.build_queries()[:self.max_queries]:
            try:
                response = requests.post(
                    _API_URL,
                    headers={'Authorization': f'Bearer {api_key}'},
                    json={
                        'api_key': api_key,
                        'query': query,
                        'topic': 'general',
                        'search_depth': connection.parameters.get('search_depth', 'basic'),
                        'max_results': 5,
                        'include_answer': False,
                        'include_images': True,
                        'include_image_descriptions': True,
                    },
                    timeout=max(3, min(int(connection.parameters.get('timeout', 20)), 60)),
                )
                if response.status_code >= 400:
                    self.last_error_code = (
                        'rate_limited' if response.status_code == 429 else 'source_error'
                    )
                    self.last_error = f'Tavily вернул HTTP {response.status_code}.'
                    logger.warning('[tavily-images] HTTP %s', response.status_code)
                    continue
                data = response.json()
            except (requests.RequestException, ValueError) as exc:
                self.last_error_code = 'source_error'
                self.last_error = f'Tavily недоступен: {exc}'
                logger.warning('[tavily-images] ошибка для %r: %s', query, exc)
                continue

            for rank, item in enumerate(data.get('images') or [], start=1):
                if isinstance(item, str):
                    url, description = item, ''
                else:
                    url = str(item.get('url') or '').strip()
                    description = strip_tags(str(item.get('description') or '')).strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                candidates.append(ImageCandidate(
                    url=url,
                    source_id=self.source_id,
                    tier=self.tier,
                    raw_meta={
                        'confidence': confidence,
                        'title': description,
                        'description': description,
                        'query': query,
                        'rank': rank,
                    },
                ))
        return candidates
