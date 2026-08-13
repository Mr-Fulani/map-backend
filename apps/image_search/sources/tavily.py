"""Image candidates returned by Tavily Search API."""

import logging
from typing import NoReturn

import requests
from django.conf import settings
from django.utils.html import strip_tags

from apps.core.http_responses import (
    TrustedResponseError, bounded_http_request, trusted_api_max_bytes,
)
from apps.core.provider_boundary import (
    TAVILY_AUTHORITATIVE_REJECTION_STATUSES,
    is_authoritative_provider_rejection,
    is_proven_pre_send_failure,
)
from apps.image_search.sources.base import (
    BaseImageSource, ImageCandidate, ImageSearchOutcomeUncertain,
)
from apps.image_search.sources.connection import (
    execute_recorded_image_search,
    image_source_api_key,
    image_source_connection,
)
from apps.image_search.sources.registry import register
from apps.web_research.providers.base import WebSearchProviderError

logger = logging.getLogger(__name__)

_API_URL = 'https://api.tavily.com/search'
_MAX_IMAGES_PER_QUERY = 5


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

    def build_workflow_plan(self, *, source_index: int) -> dict:
        plan = super().build_workflow_plan(source_index=source_index)
        connection = image_source_connection(
            self.source_id,
            getattr(self.product, 'tenant', None),
        )
        search_depth = str(connection.parameters.get('search_depth', 'basic'))
        plan['calls'] = [
            {
                'slot': f'source:{source_index}:query:{query_index}',
                'query': query,
                'confidence': confidence,
                'request_payload': {
                    'provider_id': self.provider_id,
                    'call_kind': 'image',
                    'query': query,
                    'topic': 'general',
                    'search_depth': search_depth,
                    'max_results': _MAX_IMAGES_PER_QUERY,
                    'include_answer': False,
                    'include_images': True,
                    'include_image_descriptions': True,
                },
            }
            for query_index, (query, confidence) in enumerate(
                self.build_queries()[:self.max_queries],
            )
        ]
        return plan

    def search(self) -> list[ImageCandidate]:
        plan = self.workflow_plan or self.build_workflow_plan(source_index=0)
        calls = plan.get('calls')
        if not isinstance(calls, list):
            raise ValueError('Tavily image-search workflow calls are invalid')
        candidates: list[ImageCandidate] = []
        seen_urls: set[str] = set()
        for raw_call in calls:
            if not isinstance(raw_call, dict):
                raise ValueError('Tavily image-search workflow call is invalid')
            query = raw_call.get('query')
            confidence = raw_call.get('confidence')
            slot = raw_call.get('slot')
            request_payload = raw_call.get('request_payload')
            if (
                not isinstance(query, str)
                or not isinstance(confidence, str)
                or not isinstance(slot, str)
                or not isinstance(request_payload, dict)
            ):
                raise ValueError('Tavily image-search workflow call is invalid')
            self.last_attempt_query = query
            try:
                images = execute_recorded_image_search(
                    self,
                    query=query,
                    slot=slot,
                    request_payload=request_payload,
                    call=lambda query=query, request_payload=request_payload: (
                        self._fetch_images_runtime(
                            query,
                            request_payload,
                        )
                    ),
                )
            except WebSearchProviderError as exc:
                if exc.outcome_uncertain or exc.code == 'provider_reconciliation_required':
                    raise
                self.last_error_code = exc.code
                self.last_error = str(exc)
                break

            for rank, item in enumerate(images[:_MAX_IMAGES_PER_QUERY], start=1):
                url, description = item
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

    def _fetch_images_runtime(
        self,
        query: str,
        request_payload: dict,
    ) -> list[tuple[str, str]]:
        """Resolve current credentials only after checkpoint replay missed."""
        tenant = getattr(self.product, 'tenant', None)
        connection = image_source_connection(self.source_id, tenant)
        api_key = image_source_api_key(
            self.source_id,
            tenant,
            'TAVILY_API_KEY',
            getattr(settings, 'TAVILY_API_KEY', ''),
        )
        if not api_key:
            raise WebSearchProviderError(
                'Tavily API key is not configured.',
                retryable=False,
                code='not_configured',
            )
        return self._fetch_images(
            api_key,
            connection,
            query,
            request_payload=request_payload,
        )

    def _fetch_images(
        self,
        api_key: str,
        connection,
        query: str,
        *,
        request_payload: dict | None = None,
    ) -> list[tuple[str, str]]:
        """Perform and fully validate one accounted Tavily HTTP response."""
        request_payload = request_payload or {
            'query': query,
            'topic': 'general',
            'search_depth': connection.parameters.get('search_depth', 'basic'),
            'max_results': _MAX_IMAGES_PER_QUERY,
            'include_answer': False,
            'include_images': True,
            'include_image_descriptions': True,
        }
        try:
            response = bounded_http_request(
                requests.post,
                _API_URL,
                headers={'Authorization': f'Bearer {api_key}'},
                json={
                    'api_key': api_key,
                    'query': str(request_payload.get('query') or query),
                    'topic': str(request_payload.get('topic') or 'general'),
                    'search_depth': str(
                        request_payload.get('search_depth') or 'basic'
                    ),
                    'max_results': int(
                        request_payload.get('max_results')
                        or _MAX_IMAGES_PER_QUERY
                    ),
                    'include_answer': bool(
                        request_payload.get('include_answer', False)
                    ),
                    'include_images': bool(
                        request_payload.get('include_images', True)
                    ),
                    'include_image_descriptions': bool(
                        request_payload.get(
                            'include_image_descriptions',
                            True,
                        )
                    ),
                },
                timeout=max(
                    3,
                    min(int(connection.parameters.get('timeout', 20)), 60),
                ),
                max_bytes=trusted_api_max_bytes(settings),
            )
            if not 200 <= response.status_code < 300:
                if is_authoritative_provider_rejection(
                    response.status_code,
                    documented_statuses=TAVILY_AUTHORITATIVE_REJECTION_STATUSES,
                ):
                    self.last_error_code = (
                        'authentication_error'
                        if response.status_code in {401, 403}
                        else f'http_{response.status_code}'
                    )
                    self.last_error = (
                        f'Tavily отклонил запрос (HTTP {response.status_code}).'
                    )
                    logger.warning(
                        '[tavily-images] безопасный отказ HTTP %s',
                        response.status_code,
                    )
                    return []
                self._raise_outcome_uncertain(
                    query,
                    code=f'http_{response.status_code}',
                )
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError('top-level JSON must be an object')
            images = data.get('images')
            if images is None:
                images = []
            if not isinstance(images, list):
                raise ValueError('images must be a list')

            normalized: list[tuple[str, str]] = []
            for item in images[:_MAX_IMAGES_PER_QUERY]:
                if isinstance(item, str):
                    url, description = item, ''
                elif isinstance(item, dict):
                    raw_url = item.get('url')
                    raw_description = item.get('description')
                    if raw_url is not None and not isinstance(raw_url, str):
                        raise ValueError('image URL must be a string')
                    if raw_description is not None and not isinstance(
                        raw_description,
                        str,
                    ):
                        raise ValueError('image description must be a string')
                    url = (raw_url or '').strip()
                    description = strip_tags(raw_description or '').strip()
                else:
                    raise ValueError('image result must be a string or object')
                normalized.append((url, description))
            return normalized
        except ImageSearchOutcomeUncertain:
            raise
        except TrustedResponseError as exc:
            self._raise_outcome_uncertain(
                query,
                code='invalid_response',
                cause=exc,
            )
        except requests.RequestException as exc:
            if is_proven_pre_send_failure(exc):
                self.last_error_code = 'pre_send_failure'
                self.last_error = 'Tavily недоступен до отправки запроса.'
                logger.warning(
                    '[tavily-images] запрос не был отправлен для %r',
                    query,
                )
                raise WebSearchProviderError(
                    self.last_error,
                    retryable=True,
                    code='pre_send_failure',
                ) from exc
            self._raise_outcome_uncertain(
                query,
                code='connection_error',
                cause=exc,
            )
        except (TypeError, ValueError) as exc:
            self._raise_outcome_uncertain(
                query,
                code='invalid_response',
                cause=exc,
            )
        except Exception as exc:
            self._raise_outcome_uncertain(
                query,
                code='provider_error',
                cause=exc,
            )

    def _raise_outcome_uncertain(
        self,
        query: str,
        *,
        code: str,
        cause: BaseException | None = None,
    ) -> NoReturn:
        self.last_attempt_query = query
        self.last_error_code = code
        self.last_error = 'Результат Tavily Image Search неизвестен; повтор запрещён.'
        logger.error('[tavily-images] результат запроса неизвестен для %r', query)
        if cause is None:
            raise ImageSearchOutcomeUncertain(self.last_error, code=code)
        raise ImageSearchOutcomeUncertain(self.last_error, code=code) from cause
