import re

import requests
from django.conf import settings
from django.utils.html import strip_tags

from apps.core.provider_boundary import TAVILY_AUTHORITATIVE_REJECTION_STATUSES
from apps.core.http_responses import (
    TrustedResponseError, bounded_http_request, trusted_api_max_bytes,
)
from apps.web_research.providers.base import (
    BaseWebSearchProvider, WebSearchProviderError, WebSearchResult,
    provider_http_error, provider_response_error, provider_transport_error,
)
from apps.web_research.providers.registry import register_search_provider
from apps.web_research.search_context import SearchContext


_MAX_RESULTS = 20


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
        context: SearchContext | None = None,
    ) -> dict:
        """Return Tavily's raw response for adapters that need image metadata.

        The regular web-research path deliberately exposes only normalized
        ``WebSearchResult`` objects. Catalogue adapters additionally need the
        global ``images`` collection, so they use this narrowly-scoped method
        without duplicating credentials, timeouts and error handling.
        """
        result_limit = _requested_count(count)
        if not self.api_key:
            raise WebSearchProviderError(
                'Tavily API key is not configured.', code='not_configured',
            )
        configured_domains = self.parameters.get('include_domains', [])
        try:
            response = bounded_http_request(
                requests.post,
                self.endpoint,
                json={
                    'api_key': self.api_key,
                    'query': query,
                    'topic': 'general',
                    'search_depth': self.parameters.get('search_depth', 'basic'),
                    'max_results': result_limit,
                    'include_answer': False,
                    'include_raw_content': bool(
                        self.parameters.get('include_raw_content', True)
                    ),
                    'include_domains': (
                        include_domains
                        if include_domains is not None
                        else list(context.include_domains)
                        if context and context.include_domains
                        else configured_domains
                    ),
                    'exclude_domains': (
                        list(context.exclude_domains)
                        if context else self.parameters.get('exclude_domains', [])
                    ),
                    'include_images': include_images,
                    'include_image_descriptions': include_image_descriptions,
                },
                timeout=max(3, min(int(self.parameters.get('timeout', 20)), 60)),
                max_bytes=trusted_api_max_bytes(settings),
            )
        except TrustedResponseError as exc:
            raise provider_response_error('Tavily') from exc
        except requests.RequestException as exc:
            raise provider_transport_error('Tavily', exc) from exc
        if not 200 <= response.status_code < 300:
            raise provider_http_error(
                'Tavily', response.status_code,
                documented_rejections=TAVILY_AUTHORITATIVE_REJECTION_STATUSES,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise provider_response_error('Tavily', 'invalid JSON') from exc
        if not isinstance(data, dict):
            _invalid_response('top-level JSON must be an object')

        sanitized = dict(data)
        results = _optional_list(data, 'results')
        selected_results = results[:result_limit]
        if not all(isinstance(item, dict) for item in selected_results):
            _invalid_response('results items must be objects')
        for item in selected_results:
            for key in ('title', 'url', 'content', 'raw_content', 'published_date'):
                _validate_optional_string(item, key)
            score = item.get('score')
            if (
                score is not None
                and (isinstance(score, bool) or not isinstance(score, (int, float)))
            ):
                _invalid_response('score must be numeric')
        sanitized['results'] = selected_results

        if 'images' in data and data.get('images') is not None:
            images = _optional_list(data, 'images')
            selected_images = images[:result_limit]
            if not all(isinstance(item, (str, dict)) for item in selected_images):
                _invalid_response('images items must be strings or objects')
            for item in selected_images:
                if isinstance(item, dict):
                    _validate_optional_string(item, 'url')
                    _validate_optional_string(item, 'description')
            sanitized['images'] = selected_images
        return sanitized

    def search(
        self, query: str, *, count: int = 8, context: SearchContext | None = None,
    ) -> list[WebSearchResult]:
        data = self.search_payload(query, count=count, context=context)

        results = []
        for rank, item in enumerate(data.get('results') or [], start=1):
            url = str(item.get('url') or '').strip()
            if not url:
                continue
            snippet = re.sub(
                r'\s+', ' ', strip_tags(str(item.get('content') or '')),
            ).strip()
            structured_content = str(item.get('raw_content') or '').strip()
            raw_content = re.sub(
                r'\s+', ' ', strip_tags(structured_content),
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
                raw_content=structured_content[:50000],
                rank=rank,
                score=score,
                published_at=str(item.get('published_date') or '')[:50],
                metadata={
                    'country_code': context.country_code if context else '',
                    'search_language': context.language if context else '',
                },
            ))
        return results


def _requested_count(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('count must be an integer')
    return max(1, min(value, _MAX_RESULTS))


def _optional_list(container: dict, key: str) -> list:
    value = container.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        _invalid_response(f'{key} must be a list')
    return value


def _validate_optional_string(container: dict, key: str) -> None:
    value = container.get(key)
    if value is not None and not isinstance(value, str):
        _invalid_response(f'{key} must be a string')


def _invalid_response(detail: str) -> None:
    raise provider_response_error('Tavily', detail)
