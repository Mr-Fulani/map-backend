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

    def search(self, query: str, *, count: int = 8) -> list[WebSearchResult]:
        if not self.api_key:
            raise WebSearchProviderError(
                'Tavily API key is not configured.', code='not_configured',
            )
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
                    'include_domains': self.parameters.get('include_domains', []),
                    'exclude_domains': self.parameters.get('exclude_domains', []),
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
            data = response.json()
        except ValueError as exc:
            raise WebSearchProviderError(
                'Tavily returned invalid JSON.', code='invalid_json',
            ) from exc

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
