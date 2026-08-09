import re

import requests
from django.conf import settings
from django.utils.html import strip_tags

from apps.core.http_responses import (
    TrustedResponseError, bounded_http_request, trusted_api_max_bytes,
)
from apps.web_research.providers.base import (
    BaseWebSearchProvider, WebSearchProviderError, WebSearchResult,
)
from apps.web_research.providers.registry import register_search_provider
from apps.web_research.search_context import SearchContext


_MAX_WEB_RESULTS = 20
_MAX_IMAGE_RESULTS = 200
_MAX_EXTRA_SNIPPETS = 16


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

    def search(
        self, query: str, *, count: int = 8, context: SearchContext | None = None,
    ) -> list[WebSearchResult]:
        result_limit = _requested_count(count, _MAX_WEB_RESULTS)
        api_key = self.api_key
        if not api_key:
            raise WebSearchProviderError(
                'Brave Search API key is not configured.', code='not_configured',
            )
        try:
            response = bounded_http_request(
                requests.get,
                self.endpoint,
                headers={
                    'Accept': 'application/json',
                    'X-Subscription-Token': api_key,
                },
                params={
                    'q': query,
                    'count': result_limit,
                    'country': (
                        context.country_code.lower() if context and context.country_code
                        else self.parameters.get('country', 'ru')
                    ),
                    'search_lang': (
                        context.language if context else self.parameters.get('search_lang', 'ru')
                    ),
                    'safesearch': 'moderate',
                    'extra_snippets': bool(self.parameters.get('extra_snippets', True)),
                },
                timeout=max(3, min(int(self.parameters.get('timeout', 15)), 60)),
                max_bytes=trusted_api_max_bytes(settings),
            )
        except TrustedResponseError as exc:
            raise WebSearchProviderError(
                f'Brave web search response rejected: {exc}',
                retryable=False, code='invalid_response',
            ) from exc
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
            raise WebSearchProviderError(
                'Brave web search returned invalid JSON.', code='invalid_json',
            ) from exc

        data = _response_object(data, 'Brave web search')
        web = _optional_object(data, 'web', 'Brave web search')
        provider_results = _optional_list(web, 'results', 'Brave web search')

        results = []
        for rank, item in enumerate(provider_results[:result_limit], start=1):
            if not isinstance(item, dict):
                _invalid_response('Brave web search', 'results items must be objects')
            url = _optional_string(item, 'url', 'Brave web search').strip()
            if not url:
                continue
            extra_snippets = _optional_list(
                item, 'extra_snippets', 'Brave web search',
            )
            if len(extra_snippets) > _MAX_EXTRA_SNIPPETS:
                _invalid_response(
                    'Brave web search', 'extra_snippets has too many items',
                )
            if not all(isinstance(value, str) for value in extra_snippets):
                _invalid_response(
                    'Brave web search', 'extra_snippets items must be strings',
                )
            extra = ' '.join(extra_snippets)
            description = _optional_string(item, 'description', 'Brave web search')
            title = _optional_string(item, 'title', 'Brave web search')
            snippet = strip_tags(' '.join(filter(None, [description, extra])))
            snippet = re.sub(r'\s+', ' ', snippet).strip()
            results.append(WebSearchResult(
                title=strip_tags(title)[:500],
                url=url,
                snippet=snippet[:2000],
                rank=rank,
                metadata={
                    'country_code': context.country_code if context else '',
                    'search_language': context.language if context else '',
                },
            ))
        return results

    def search_images(self, query: str, *, count: int = 50) -> list[dict]:
        """Return image results with their source page URL.

        Catalogue adapters use the source page to prove that an image belongs
        to the exact product, rather than accepting a visually similar result.
        """
        result_limit = _requested_count(count, _MAX_IMAGE_RESULTS)
        if not self.api_key:
            raise WebSearchProviderError(
                'Brave Search API key is not configured.', code='not_configured',
            )
        try:
            response = bounded_http_request(
                requests.get,
                self.image_endpoint,
                headers={
                    'Accept': 'application/json',
                    'X-Subscription-Token': self.api_key,
                },
                params={
                    'q': query,
                    'count': result_limit,
                    'country': self.parameters.get('country', 'ru'),
                    'search_lang': self.parameters.get('search_lang', 'ru'),
                    'safesearch': 'strict',
                    'spellcheck': False,
                },
                timeout=max(3, min(int(self.parameters.get('timeout', 15)), 60)),
                max_bytes=trusted_api_max_bytes(settings),
            )
        except TrustedResponseError as exc:
            raise WebSearchProviderError(
                f'Brave image search response rejected: {exc}',
                retryable=False, code='invalid_response',
            ) from exc
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

        data = _response_object(data, 'Brave image search')
        provider_results = _optional_list(data, 'results', 'Brave image search')
        selected_results = provider_results[:result_limit]
        if not all(isinstance(item, dict) for item in selected_results):
            _invalid_response('Brave image search', 'results items must be objects')
        for item in selected_results:
            _optional_string(item, 'url', 'Brave image search')
            _optional_string(item, 'title', 'Brave image search')
            properties = _optional_object(item, 'properties', 'Brave image search')
            _optional_string(properties, 'url', 'Brave image search')

        from apps.image_search.sources.brave import BraveImageSource
        BraveImageSource._track_quota(response)
        return selected_results


def _requested_count(value: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('count must be an integer')
    return max(1, min(value, maximum))


def _response_object(value, provider_name: str) -> dict:
    if not isinstance(value, dict):
        _invalid_response(provider_name, 'top-level JSON must be an object')
    return value


def _optional_object(container: dict, key: str, provider_name: str) -> dict:
    value = container.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        _invalid_response(provider_name, f'{key} must be an object')
    return value


def _optional_list(container: dict, key: str, provider_name: str) -> list:
    value = container.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        _invalid_response(provider_name, f'{key} must be a list')
    return value


def _optional_string(container: dict, key: str, provider_name: str) -> str:
    value = container.get(key)
    if value is None:
        return ''
    if not isinstance(value, str):
        _invalid_response(provider_name, f'{key} must be a string')
    return value


def _invalid_response(provider_name: str, detail: str) -> None:
    raise WebSearchProviderError(
        f'{provider_name} returned an invalid response: {detail}.',
        retryable=False,
        code='invalid_response',
    )
