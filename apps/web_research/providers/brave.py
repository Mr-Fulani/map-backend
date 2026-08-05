import re

import requests
from django.conf import settings
from django.utils.html import strip_tags

from apps.web_research.providers.base import (
    BaseWebSearchProvider, WebSearchProviderError, WebSearchResult,
)
from apps.web_research.providers.registry import register_search_provider


@register_search_provider
class BraveWebSearchProvider(BaseWebSearchProvider):
    """Brave Web Search adapter; business logic depends only on the base interface."""

    provider_id = 'brave'
    endpoint = 'https://api.search.brave.com/res/v1/web/search'

    def is_available(self) -> bool:
        return bool(getattr(settings, 'BRAVE_SEARCH_API_KEY', ''))

    def search(self, query: str, *, count: int = 8) -> list[WebSearchResult]:
        try:
            response = requests.get(
                self.endpoint,
                headers={
                    'Accept': 'application/json',
                    'Accept-Encoding': 'gzip',
                    'X-Subscription-Token': settings.BRAVE_SEARCH_API_KEY,
                },
                params={
                    'q': query,
                    'count': max(1, min(count, 20)),
                    'country': 'ru',
                    'search_lang': 'ru',
                    'safesearch': 'moderate',
                },
                timeout=15,
            )
        except requests.RequestException as exc:
            raise WebSearchProviderError(
                f'Brave web search connection error: {exc}', retryable=True,
            ) from exc
        if response.status_code >= 400:
            raise WebSearchProviderError(
                f'Brave web search HTTP {response.status_code}',
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise WebSearchProviderError('Brave web search returned invalid JSON') from exc

        results = []
        for rank, item in enumerate((data.get('web') or {}).get('results') or [], start=1):
            url = str(item.get('url') or '').strip()
            if not url:
                continue
            snippet = strip_tags(str(item.get('description') or ''))
            snippet = re.sub(r'\s+', ' ', snippet).strip()
            results.append(WebSearchResult(
                title=strip_tags(str(item.get('title') or ''))[:500],
                url=url,
                snippet=snippet[:2000],
                rank=rank,
            ))
        return results
