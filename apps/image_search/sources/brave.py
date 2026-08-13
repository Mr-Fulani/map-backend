"""Источник изображений: Brave Search API (Tier 3, платный).

Официальный REST API — не подвержен IP-банам как DDG.
Документация: https://api.search.brave.com/app/documentation/image-search/query
Квота: 1000 запросов/месяц бесплатно, затем $5/мес. Soft cap = 800.
"""

import logging
from typing import NoReturn

import requests
from django.conf import settings

from apps.core.http_responses import (
    TrustedResponseError, bounded_http_request, trusted_api_max_bytes,
)
from apps.core.provider_boundary import (
    BRAVE_AUTHORITATIVE_REJECTION_STATUSES,
    is_authoritative_provider_rejection,
    is_proven_pre_send_failure,
)
from apps.image_search.models import BraveQuota
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
        tenant = getattr(self.product, 'tenant', None)
        connection = image_source_connection(self.source_id, tenant)
        if not connection.enabled:
            return False
        if not image_source_api_key(
            self.source_id, tenant, 'BRAVE_SEARCH_API_KEY',
            getattr(settings, 'BRAVE_SEARCH_API_KEY', ''),
        ):
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

    def build_workflow_plan(self, *, source_index: int) -> dict:
        plan = super().build_workflow_plan(source_index=source_index)
        plan['calls'] = [
            {
                'slot': f'source:{source_index}:query:{query_index}',
                'query': query,
                'confidence': confidence,
                'request_payload': {
                    'provider_id': self.provider_id,
                    'call_kind': 'image',
                    'query': query,
                    'count': _MAX_RESULTS_PER_QUERY,
                    'search_lang': 'ru',
                    'country': 'ru',
                    'safesearch': 'off',
                },
            }
            for query_index, (query, confidence) in enumerate(
                self.build_queries()[:self.max_queries],
            )
        ]
        return plan

    def search(self) -> list[ImageCandidate]:
        """Ищет изображения через Brave Search API.

        Returns:
            Список ImageCandidate с width/height и confidence в raw_meta.
        """
        plan = self.workflow_plan or self.build_workflow_plan(source_index=0)
        calls = plan.get('calls')
        if not isinstance(calls, list):
            raise ValueError('Brave image-search workflow calls are invalid')
        candidates: list[ImageCandidate] = []
        seen_urls: set[str] = set()

        for raw_call in calls:
            if not isinstance(raw_call, dict):
                raise ValueError('Brave image-search workflow call is invalid')
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
                raise ValueError('Brave image-search workflow call is invalid')
            self.last_attempt_query = query
            try:
                results = execute_recorded_image_search(
                    self,
                    query=query,
                    slot=slot,
                    request_payload=request_payload,
                    call=lambda query=query, request_payload=request_payload: (
                        self._fetch_runtime(query, request_payload)
                    ),
                )
            except WebSearchProviderError as exc:
                if exc.outcome_uncertain or exc.code == 'provider_reconciliation_required':
                    raise
                self.last_error_code = exc.code
                self.last_error = str(exc)
                break
            for rank, r in enumerate(results, start=1):
                props = r.get('properties') or {}
                thumb = r.get('thumbnail') or {}
                url = props.get('url', '')
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
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

    def _fetch_runtime(self, query: str, request_payload: dict) -> list[dict]:
        """Resolve current credentials only after checkpoint replay missed."""
        api_key = image_source_api_key(
            self.source_id,
            getattr(self.product, 'tenant', None),
            'BRAVE_SEARCH_API_KEY',
            getattr(settings, 'BRAVE_SEARCH_API_KEY', ''),
        )
        if not api_key:
            raise WebSearchProviderError(
                'Brave Image Search API key is not configured.',
                retryable=False,
                code='not_configured',
            )
        return self._fetch(api_key, query, request_payload=request_payload)

    def _fetch(
        self,
        api_key: str,
        query: str,
        *,
        request_payload: dict | None = None,
    ) -> list[dict]:
        """Выполняет один HTTP-запрос к Brave Image Search API."""
        self.last_attempt_query = query
        request_payload = request_payload or {
            'query': query,
            'count': _MAX_RESULTS_PER_QUERY,
            'search_lang': 'ru',
            'country': 'ru',
            'safesearch': 'off',
        }
        try:
            resp = bounded_http_request(
                requests.get,
                _API_URL,
                headers={
                    'Accept': 'application/json',
                    'X-Subscription-Token': api_key,
                },
                params={
                    'q': str(request_payload.get('query') or query),
                    'count': int(
                        request_payload.get('count')
                        or _MAX_RESULTS_PER_QUERY
                    ),
                    'search_lang': str(
                        request_payload.get('search_lang') or 'ru'
                    ),
                    'country': str(request_payload.get('country') or 'ru'),
                    'safesearch': str(
                        request_payload.get('safesearch') or 'off'
                    ),
                },
                timeout=_TIMEOUT_SEC,
                max_bytes=trusted_api_max_bytes(settings),
            )

            if not 200 <= resp.status_code < 300:
                if is_authoritative_provider_rejection(
                    resp.status_code,
                    documented_statuses=BRAVE_AUTHORITATIVE_REJECTION_STATUSES,
                ):
                    self.last_error_code = (
                        'authentication_error'
                        if resp.status_code in {401, 403}
                        else f'http_{resp.status_code}'
                    )
                    self.last_error = f'Brave отклонил запрос (HTTP {resp.status_code}).'
                    logger.warning('[brave] безопасный отказ HTTP %s', resp.status_code)
                    return []
                self._raise_outcome_uncertain(
                    query,
                    code=f'http_{resp.status_code}',
                )

            # A valid 2xx response proves the request was accepted and billed.
            self._track_quota(resp)

            payload = resp.json()
            if not isinstance(payload, dict):
                raise ValueError('top-level JSON must be an object')
            results = payload.get('results')
            if results is None:
                return []
            if not isinstance(results, list):
                raise ValueError('results must be a list')

            selected = results[:_MAX_RESULTS_PER_QUERY]
            for item in selected:
                if not isinstance(item, dict):
                    raise ValueError('results items must be objects')
                properties = item.get('properties')
                thumbnail = item.get('thumbnail')
                if properties is not None and not isinstance(properties, dict):
                    raise ValueError('properties must be an object')
                if thumbnail is not None and not isinstance(thumbnail, dict):
                    raise ValueError('thumbnail must be an object')
                url = (properties or {}).get('url')
                if url is not None and not isinstance(url, str):
                    raise ValueError('properties.url must be a string')
                title = item.get('title')
                if title is not None and not isinstance(title, str):
                    raise ValueError('title must be a string')
                for dimensions in (properties or {}, thumbnail or {}):
                    for field_name in ('width', 'height'):
                        dimension = dimensions.get(field_name)
                        if (
                            dimension is not None
                            and (
                                isinstance(dimension, bool)
                                or not isinstance(dimension, int)
                                or dimension < 0
                            )
                        ):
                            raise ValueError(
                                f'{field_name} must be a non-negative integer',
                            )
            return selected
        except ImageSearchOutcomeUncertain:
            raise
        except TrustedResponseError as exc:
            self._raise_outcome_uncertain(query, code='invalid_response', cause=exc)
        except requests.RequestException as exc:
            if is_proven_pre_send_failure(exc):
                self.last_error = 'Brave недоступен до отправки запроса.'
                self.last_error_code = 'pre_send_failure'
                logger.warning('[brave] запрос не был отправлен для %r', query)
                raise WebSearchProviderError(
                    self.last_error,
                    retryable=True,
                    code='pre_send_failure',
                ) from exc
            self._raise_outcome_uncertain(query, code='connection_error', cause=exc)
        except (TypeError, ValueError) as exc:
            self._raise_outcome_uncertain(query, code='invalid_response', cause=exc)
        except Exception as exc:
            self._raise_outcome_uncertain(query, code='provider_error', cause=exc)

    def _raise_outcome_uncertain(
        self,
        query: str,
        *,
        code: str,
        cause: BaseException | None = None,
    ) -> NoReturn:
        self.last_attempt_query = query
        self.last_error_code = code
        self.last_error = 'Результат Brave Image Search неизвестен; повтор запрещён.'
        logger.error('[brave] результат запроса неизвестен для %r', query)
        if cause is None:
            raise ImageSearchOutcomeUncertain(self.last_error, code=code)
        raise ImageSearchOutcomeUncertain(self.last_error, code=code) from cause

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
