import re

import requests
from django.conf import settings
from django.utils.html import strip_tags

from apps.web_research.providers.base import (
    BaseWebSearchProvider, WebSearchProviderError, WebSearchResult,
)
from apps.web_research.providers.registry import register_search_provider


@register_search_provider
class TavilyWebSearchProvider(BaseWebSearchProvider):
    """Tavily adapter with optional cleaned page content for grounded extraction."""

    provider_id = 'tavily'
    display_name = 'Tavily'
    endpoint = 'https://api.tavily.com/search'

    @property
    def api_key(self) -> str:
        return str(
            self.credentials.get('api_key')
            or getattr(settings, 'TAVILY_API_KEY', '')
        )

    def is_available(self) -> bool:
        return bool(self.api_key)

    def search_payload(
        self,
        query: str,
        *,
        count: int = 8,
        include_domains: list[str] | None = None,
        include_images: bool = False,
        include_image_descriptions: bool = False,
    ) -> dict:
        """Return Tavily's raw response for adapters that need image metadata.

        The regular web-research path deliberately exposes only normalized
        ``WebSearchResult`` objects. Catalogue adapters additionally need the
        global ``images`` collection, so they use this narrowly-scoped method
        without duplicating credentials, timeouts and error handling.
        """
        if not self.api_key:
            raise WebSearchProviderError(
                'Tavily API key is not configured.', code='not_configured',
            )
        configured_domains = self.parameters.get('include_domains', [])
        try:
            response = requests.post(
                self.endpoint,
                json={
                    'api_key': self.api_key,
                    'query': query,
                    'topic': 'general',
                    'search_depth': self.parameters.get('search_depth', 'basic'),
                    'max_results': max(1, min(count, 20)),
                    'include_answer': False,
                    'include_raw_content': bool(
                        self.parameters.get('include_raw_content', True)
                    ),
                    'include_domains': (
                        include_domains
                        if include_domains is not None
                        else configured_domains
                    ),
                    'exclude_domains': self.parameters.get('exclude_domains', []),
                    'include_images': include_images,
                    'include_image_descriptions': include_image_descriptions,
                },
                timeout=max(3, min(int(self.parameters.get('timeout', 20)), 60)),
            )
        except requests.RequestException as exc:
            raise WebSearchProviderError(
                f'Tavily connection error: {exc}',
                retryable=True, code='connection_error',
            ) from exc
        if response.status_code >= 400:
            raise WebSearchProviderError(
                f'Tavily HTTP {response.status_code}',
                retryable=response.status_code == 429 or response.status_code >= 500,
                code=f'http_{response.status_code}',
            )
        try:
            return response.json()
        except ValueError as exc:
            raise WebSearchProviderError(
                'Tavily returned invalid JSON.', code='invalid_json',
            ) from exc

    def search(self, query: str, *, count: int = 8) -> list[WebSearchResult]:
        data = self.search_payload(query, count=count)

        results = []
        for rank, item in enumerate(data.get('results') or [], start=1):
            url = str(item.get('url') or '').strip()
            if not url:
                continue
            snippet = re.sub(
                r'\s+', ' ', strip_tags(str(item.get('content') or '')),
            ).strip()
            raw_content = re.sub(
                r'\s+', ' ', strip_tags(str(item.get('raw_content') or '')),
            ).strip()
            try:
                score = float(item['score']) if item.get('score') is not None else None
            except (TypeError, ValueError):
                score = None
            results.append(WebSearchResult(
                title=strip_tags(str(item.get('title') or ''))[:500],
                url=url,
                snippet=snippet[:2000],
                content=raw_content[:12000],
                rank=rank,
                score=score,
                published_at=str(item.get('published_date') or '')[:50],
            ))
        return results
