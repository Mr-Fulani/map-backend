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
    display_name = 'Brave Search'
    endpoint = 'https://api.search.brave.com/res/v1/web/search'
    image_endpoint = 'https://api.search.brave.com/res/v1/images/search'

    @property
    def api_key(self) -> str:
        return str(
            self.credentials.get('api_key')
            or getattr(settings, 'BRAVE_SEARCH_API_KEY', '')
        )

    def is_available(self) -> bool:
        return bool(self.api_key)

    def search(self, query: str, *, count: int = 8) -> list[WebSearchResult]:
        api_key = self.api_key
        if not api_key:
            raise WebSearchProviderError(
                'Brave Search API key is not configured.', code='not_configured',
            )
        try:
            response = requests.get(
                self.endpoint,
                headers={
                    'Accept': 'application/json',
                    'Accept-Encoding': 'gzip',
                    'X-Subscription-Token': api_key,
                },
                params={
                    'q': query,
                    'count': max(1, min(count, 20)),
                    'country': self.parameters.get('country', 'ru'),
                    'search_lang': self.parameters.get('search_lang', 'ru'),
                    'safesearch': 'moderate',
                    'extra_snippets': bool(self.parameters.get('extra_snippets', True)),
                },
                timeout=max(3, min(int(self.parameters.get('timeout', 15)), 60)),
            )
        except requests.RequestException as exc:
            raise WebSearchProviderError(
                f'Brave web search connection error: {exc}',
                retryable=True, code='connection_error',
            ) from exc
        if response.status_code >= 400:
            raise WebSearchProviderError(
                f'Brave web search HTTP {response.status_code}',
                retryable=response.status_code == 429 or response.status_code >= 500,
                code=f'http_{response.status_code}',
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
            extra = ' '.join(str(value) for value in item.get('extra_snippets') or [])
            snippet = strip_tags(' '.join(filter(None, [str(item.get('description') or ''), extra])))
            snippet = re.sub(r'\s+', ' ', snippet).strip()
            results.append(WebSearchResult(
                title=strip_tags(str(item.get('title') or ''))[:500],
                url=url,
                snippet=snippet[:2000],
                rank=rank,
            ))
        return results

    def search_images(self, query: str, *, count: int = 50) -> list[dict]:
        """Return image results with their source page URL.

        Catalogue adapters use the source page to prove that an image belongs
        to the exact product, rather than accepting a visually similar result.
        """
        if not self.api_key:
            raise WebSearchProviderError(
                'Brave Search API key is not configured.', code='not_configured',
            )
        try:
            response = requests.get(
                self.image_endpoint,
                headers={
                    'Accept': 'application/json',
                    'Accept-Encoding': 'gzip',
                    'X-Subscription-Token': self.api_key,
                },
                params={
                    'q': query,
                    'count': max(1, min(count, 200)),
                    'country': self.parameters.get('country', 'ru'),
                    'search_lang': self.parameters.get('search_lang', 'ru'),
                    'safesearch': 'strict',
                    'spellcheck': False,
                },
                timeout=max(3, min(int(self.parameters.get('timeout', 15)), 60)),
            )
        except requests.RequestException as exc:
            raise WebSearchProviderError(
                f'Brave image search connection error: {exc}',
                retryable=True,
                code='connection_error',
            ) from exc
        if response.status_code >= 400:
            raise WebSearchProviderError(
                f'Brave image search HTTP {response.status_code}',
                retryable=response.status_code == 429 or response.status_code >= 500,
                code=f'http_{response.status_code}',
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise WebSearchProviderError(
                'Brave image search returned invalid JSON.', code='invalid_json',
            ) from exc

        from apps.image_search.sources.brave import BraveImageSource
        BraveImageSource._track_quota(response)
        return data.get('results') or []
