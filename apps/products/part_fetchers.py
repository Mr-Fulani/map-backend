import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol, TypeGuard
from urllib.parse import parse_qs, urlparse

import requests
from django.conf import settings

from apps.core.url_security import (
    REDIRECT_NONE,
    UnsafePublicURL,
    request_public_http_url,
)
from apps.products.source_policy import get_part_source_policy


@dataclass(frozen=True)
class FetchedPage:
    html: str
    url: str
    status_code: int
    response: requests.Response | None = None

    def raise_for_status(self) -> None:
        if self.response is not None:
            self.response.raise_for_status()
            return
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            response.url = self.url
            response.raise_for_status()


class ImageSearchProvider(Protocol):
    def search_images(self, query: str, *, count: int = 50) -> list[dict]: ...


def _supports_image_search(provider: object) -> TypeGuard[ImageSearchProvider]:
    return callable(getattr(provider, 'search_images', None))


class HttpxPartFetcher:
    """Compatibility name for the DNS-pinned catalogue HTTP transport."""

    user_agent = 'MAP enrichment bot (+https://map.local)'

    def fetch(self, url: str) -> FetchedPage:
        response = request_public_http_url(
            url,
            timeout=(5, 20),
            headers={'User-Agent': self.user_agent},
            max_response_bytes=settings.PART_PAGE_MAX_BYTES,
        )
        encoding = response.encoding
        if not encoding or encoding.lower() == 'iso-8859-1':
            encoding = response.apparent_encoding or 'utf-8'
        return FetchedPage(
            html=response.content.decode(encoding, errors='replace'),
            url=str(response.url),
            status_code=response.status_code,
            response=response,
        )


def build_euroauto_workflow_snapshot(
    tenant,
    *,
    article: str,
    hint: str,
    brand: str = '',
) -> dict:
    """Freeze provider order and public request bytes before any paid call."""
    from apps.web_research.routing import search_provider_candidates

    normalized_article = str(article).strip()
    normalized_hint = str(hint).strip()
    query = ' '.join(filter(None, [
        'site:euroauto.ru',
        f'"{normalized_article}"',
        normalized_hint,
        'автозапчасть',
    ]))
    providers = []
    for index, candidate in enumerate(search_provider_candidates(tenant)):
        provider = candidate.provider
        parameters = getattr(provider, 'parameters', {})
        if not isinstance(parameters, dict):
            parameters = {}
        exclude_domains = parameters.get('exclude_domains', [])
        if not isinstance(exclude_domains, list):
            exclude_domains = []
        provider_id = str(provider.provider_id)
        if provider_id == 'tavily' and hasattr(provider, 'search_payload'):
            text_payload = {
                'provider_id': provider_id,
                'call_kind': 'text',
                'query': query,
                'topic': 'general',
                'search_depth': str(parameters.get('search_depth', 'basic')),
                'count': 10,
                'include_answer': False,
                'include_raw_content': bool(
                    parameters.get('include_raw_content', True)
                ),
                'include_domains': ['euroauto.ru'],
                'exclude_domains': [str(value) for value in exclude_domains[:50]],
                'include_images': False,
            }
            text_codec = 'tavily_payload'
        else:
            text_payload = {
                'provider_id': provider_id,
                'call_kind': 'text',
                'query': query,
                'count': 10,
                'country': str(parameters.get('country', 'ru')),
                'search_lang': str(parameters.get('search_lang', 'ru')),
                'safesearch': 'moderate',
                'extra_snippets': bool(parameters.get('extra_snippets', True)),
            }
            text_codec = 'web_results'
        provider_plan = {
            'provider_id': provider_id,
            'connection_id': getattr(candidate.connection, 'pk', None),
            'text': {
                'slot': f'text:{index}',
                'codec': text_codec,
                'request_payload': text_payload,
            },
        }
        if provider_id == 'brave' and _supports_image_search(provider):
            provider_plan['image'] = {
                'slot': f'image:{index}',
                'codec': 'json',
                'request_payload': {
                    'provider_id': provider_id,
                    'call_kind': 'image',
                    'query': query,
                    'count': 50,
                    'country': str(parameters.get('country', 'ru')),
                    'search_lang': str(parameters.get('search_lang', 'ru')),
                    'safesearch': 'strict',
                    'spellcheck': False,
                },
            }
        providers.append(provider_plan)
    return {
        'version': 1,
        'kind': 'euroauto_search',
        'article': normalized_article,
        'hint': normalized_hint,
        'brand': str(brand).strip(),
        'query': query,
        'providers': providers,
    }


class EuroautoSearchFetcher:
    """Fetch Euroauto evidence through managed web-search connections.

    Euroauto protects product pages with a Qrator JavaScript challenge, which
    makes a plain server-side HTTP client unreliable. The platform's configured
    Brave/Tavily order is used for text. Images are accepted only when Brave's
    image result links them to the exact Euroauto product page.
    """

    source_id = 'euroauto'
    domain = 'euroauto.ru'
    image_host = 'file.euroauto.ru'

    def __init__(self, tenant=None):
        self.tenant = tenant
        self.domain_reference = ''
        self.web_search_workflow = None
        self.consumed_attempt_ids: set[int] = set()

    def set_domain_reference(self, domain_reference: str) -> None:
        self.domain_reference = str(domain_reference).strip()[:160]

    def set_web_search_workflow(self, workflow) -> None:
        self.web_search_workflow = workflow
        self.consumed_attempt_ids = set()

    def get_consumed_attempt_ids(self) -> set[int]:
        return set(self.consumed_attempt_ids)

    def _consume_attempt(self, attempt_id: object) -> None:
        if isinstance(attempt_id, int) and not isinstance(attempt_id, bool):
            self.consumed_attempt_ids.add(attempt_id)

    def _workflow_snapshot(self) -> dict:
        snapshot = getattr(self.web_search_workflow, 'input_snapshot', None)
        if (
            not isinstance(snapshot, dict)
            or snapshot.get('version') != 1
            or snapshot.get('kind') != 'euroauto_search'
            or not isinstance(snapshot.get('providers'), list)
        ):
            from apps.web_research.providers.base import WebSearchProviderError
            raise WebSearchProviderError(
                'Euroauto paid-search workflow plan is invalid.',
                code='provider_request_conflict',
                outcome_uncertain=True,
            )
        return snapshot

    def _resolve_planned_candidate(self, provider_plan: dict):
        """Resolve only the exact snapshotted provider after replay misses."""
        from apps.web_research.providers.base import WebSearchProviderError
        from apps.web_research.routing import search_provider_candidates

        provider_id = provider_plan.get('provider_id')
        connection_id = provider_plan.get('connection_id')
        candidates = search_provider_candidates(
            self.tenant,
            requested_provider=str(provider_id or ''),
        )
        for candidate in candidates:
            candidate_connection_id = getattr(candidate.connection, 'pk', None)
            if (
                candidate.provider.provider_id == provider_id
                and candidate_connection_id == connection_id
            ):
                return candidate
        raise WebSearchProviderError(
            'Snapshotted Euroauto search provider is unavailable before send.',
            retryable=False,
            code='provider_unavailable',
        )

    @staticmethod
    def _freeze_provider_for_call(candidate, request_payload: dict):
        """Bind actual request parameters to the fingerprinted public payload.

        Credentials and transport timeout remain runtime operational settings;
        every public provider request byte that affects paid search identity is
        reconstructed from the immutable workflow snapshot.
        """
        provider = candidate.provider
        from apps.web_research.providers.base import BaseWebSearchProvider

        if not isinstance(provider, BaseWebSearchProvider):
            # Pure parser tests intentionally use protocol mocks. Runtime
            # routing always returns a registered BaseWebSearchProvider.
            return provider
        frozen_parameters = {
            'country': str(request_payload.get('country') or 'ru'),
            'search_lang': str(request_payload.get('search_lang') or 'ru'),
            'extra_snippets': bool(
                request_payload.get('extra_snippets', True),
            ),
            'search_depth': str(
                request_payload.get('search_depth') or 'basic',
            ),
            'include_raw_content': bool(
                request_payload.get('include_raw_content', True),
            ),
            'include_domains': list(
                request_payload.get('include_domains') or [],
            ),
            'exclude_domains': list(
                request_payload.get('exclude_domains') or [],
            ),
            # Timeout is deliberately operational rather than request identity,
            # but bind the current validated value onto the per-call clone.
            'timeout': max(
                3,
                min(int(getattr(provider, 'parameters', {}).get('timeout', 20)), 60),
            ),
        }
        return type(provider)(
            credentials=dict(getattr(provider, 'credentials', {}) or {}),
            parameters=frozen_parameters,
        )

    def fetch(self, url: str) -> FetchedPage:
        from apps.web_research.providers.base import (
            WebSearchProviderError,
            WebSearchResult,
        )
        from apps.web_research.accounting import (
            deterministic_web_search_call_key,
            execute_recorded_web_search,
            fingerprint_web_search_request,
            replay_recorded_web_search,
        )

        workflow = self.web_search_workflow
        if workflow is None:
            raise WebSearchProviderError(
                'Durable Euroauto paid-search workflow is required.',
                code='provider_request_conflict',
                outcome_uncertain=True,
            )

        def recorded_call(
            *, provider_plan, call_plan, call_kind, call_builder,
            normalize_result=lambda value: value,
            restore_result=lambda value: value,
            result_count=len,
        ):
            provider_id = provider_plan.get('provider_id')
            slot = call_plan.get('slot')
            request_payload = call_plan.get('request_payload')
            if (
                not isinstance(provider_id, str)
                or not isinstance(slot, str)
                or not isinstance(request_payload, dict)
            ):
                raise WebSearchProviderError(
                    'Euroauto paid-search call plan is invalid.',
                    code='provider_request_conflict',
                    outcome_uncertain=True,
                )
            request_fingerprint = fingerprint_web_search_request(request_payload)
            call_key = deterministic_web_search_call_key(
                provider_id=provider_id,
                call_kind=call_kind,
                slot=slot,
            )
            try:
                replay = replay_recorded_web_search(
                    workflow,
                    call_key=call_key,
                    request_fingerprint=request_fingerprint,
                    restore_result=restore_result,
                )
            except WebSearchProviderError as exc:
                self._consume_attempt(getattr(exc, 'attempt_id', None))
                raise
            if replay is not None:
                self._consume_attempt(replay.attempt_id)
                return replay.result

            # No checkpoint exists for this logical slot. Only now may runtime
            # credentials/enablement be inspected; never substitute a newly
            # preferred provider for the immutable plan.
            candidate = self._resolve_planned_candidate(provider_plan)
            provider = self._freeze_provider_for_call(candidate, request_payload)
            try:
                execution = execute_recorded_web_search(
                    workflow=workflow,
                    provider=provider,
                    connection=candidate.connection,
                    query=str(request_payload.get('query') or ''),
                    call_key=call_key,
                    request_fingerprint=request_fingerprint,
                    call=call_builder(provider, request_payload),
                    call_kind=call_kind,
                    normalize_result=normalize_result,
                    restore_result=restore_result,
                    result_count=result_count,
                )
            except WebSearchProviderError as exc:
                self._consume_attempt(getattr(exc, 'attempt_id', None))
                raise
            self._consume_attempt(getattr(execution, 'attempt_id', None))
            return getattr(execution, 'result', execution)

        params = parse_qs(urlparse(url).query)
        requested_article = (params.get('q') or [''])[0].strip()
        if not requested_article:
            raise ValueError('Euroauto search requires an article.')
        snapshot = self._workflow_snapshot()
        article = snapshot.get('article')
        hint = snapshot.get('hint')
        query = snapshot.get('query')
        provider_plans = snapshot.get('providers')
        if (
            not isinstance(article, str)
            or not article
            or not isinstance(hint, str)
            or not isinstance(query, str)
            or not isinstance(provider_plans, list)
        ):
            raise WebSearchProviderError(
                'Euroauto paid-search workflow input is invalid.',
                code='provider_request_conflict',
                outcome_uncertain=True,
            )
        if not provider_plans:
            raise RuntimeError(
                'Для каталога Euroauto требуется активное подключение Brave или Tavily.'
            )
        payload: dict[str, Any] = {'results': [], 'images': []}
        last_error: Exception | None = None
        for provider_plan in provider_plans:
            if not isinstance(provider_plan, dict):
                raise WebSearchProviderError(
                    'Euroauto paid-search provider plan is invalid.',
                    code='provider_request_conflict',
                    outcome_uncertain=True,
                )
            text_plan = provider_plan.get('text')
            if not isinstance(text_plan, dict):
                raise WebSearchProviderError(
                    'Euroauto paid-search text plan is invalid.',
                    code='provider_request_conflict',
                    outcome_uncertain=True,
                )
            provider_id = provider_plan.get('provider_id')
            try:
                if text_plan.get('codec') == 'tavily_payload':
                    provider_payload = recorded_call(
                        provider_plan=provider_plan,
                        call_plan=text_plan,
                        call_kind='text',
                        call_builder=lambda provider, request: (
                            lambda: provider.search_payload(
                                str(request['query']),
                                count=int(request['count']),
                                include_domains=list(request['include_domains']),
                            )
                        ),
                        result_count=lambda data: len(data.get('results') or []),
                    )
                    payload['results'].extend(provider_payload.get('results') or [])
                elif text_plan.get('codec') == 'web_results':
                    provider_results = recorded_call(
                        provider_plan=provider_plan,
                        call_plan=text_plan,
                        call_kind='text',
                        call_builder=lambda provider, request: (
                            lambda: provider.search(
                                str(request['query']),
                                count=int(request['count']),
                            )
                        ),
                        normalize_result=lambda results: [
                            asdict(result) for result in results
                        ],
                        restore_result=lambda results: [
                            WebSearchResult(**result) for result in results
                        ],
                    )
                    payload['results'].extend([
                        {
                            'url': result.url,
                            'title': result.title,
                            'content': ' '.join(filter(None, [
                                result.snippet,
                                result.content,
                            ])),
                            'score': result.score,
                            'provider_id': provider_id,
                        }
                        for result in provider_results
                    ])
                else:
                    raise WebSearchProviderError(
                        'Euroauto paid-search result codec is invalid.',
                        code='provider_request_conflict',
                        outcome_uncertain=True,
                    )
            except WebSearchProviderError as exc:
                if exc.outcome_uncertain:
                    raise
                last_error = exc
                continue
            if self._best_source_url(payload, article):
                break

        if not self._best_source_url(payload, article) and last_error:
            raise last_error

        image_provider_plan = None
        image_plan = None
        for provider_plan in provider_plans:
            if isinstance(provider_plan, dict) and isinstance(
                provider_plan.get('image'),
                dict,
            ):
                image_provider_plan = provider_plan
                image_plan = provider_plan['image']
                break
        if image_provider_plan is not None and image_plan is not None:
            try:
                payload['images'] = recorded_call(
                    provider_plan=image_provider_plan,
                    call_plan=image_plan,
                    call_kind='image',
                    call_builder=lambda provider, request: (
                        lambda: provider.search_images(
                            str(request['query']),
                            count=int(request['count']),
                        )
                    ),
                )
            except WebSearchProviderError as exc:
                if (
                    exc.outcome_uncertain
                    or exc.code == 'provider_reconciliation_required'
                ):
                    raise
                payload['images'] = []
        payload['_map_request'] = {'article': article, 'hint': hint}
        confirmed_image = self._confirmed_product_image(
            payload.get('images') or [], article,
        )
        product_id = confirmed_image[0] if confirmed_image else ''
        if confirmed_image and not self._best_source_url(payload, article):
            image = confirmed_image[1]
            payload['results'].append({
                'url': str(image.get('url') or ''),
                'title': str(image.get('title') or ''),
                'content': str(image.get('title') or ''),
                'score': 1.0,
                'provider_id': 'brave_images',
            })
        payload['_map_product_images'] = (
            self._discover_product_images(product_id) if product_id else []
        )
        source_url = self._best_source_url(payload, article) or url
        return FetchedPage(
            html=json.dumps(payload, ensure_ascii=False),
            url=source_url,
            status_code=200,
        )

    def _discover_product_images(self, product_id: str) -> list[str]:
        first_url = (
            f'https://{self.image_host}/v2/file/parts/new/{product_id}/1.jpg'
        )
        prefix = first_url.rsplit('/', 1)[0]
        discovered = []
        for number in range(1, 11):
            candidate = f'{prefix}/{number}.jpg'
            try:
                response = request_public_http_url(
                    candidate,
                    method='HEAD',
                    timeout=(3, 8),
                    headers={'User-Agent': HttpxPartFetcher.user_agent},
                    status_only=True,
                    redirect_policy=REDIRECT_NONE,
                )
            except (requests.RequestException, UnsafePublicURL):
                break
            if response.status_code == 200:
                discovered.append(candidate)
                continue
            if number > 1:
                break
        return discovered or [first_url]

    @staticmethod
    def _confirmed_product_id(images: list[dict], article: str) -> str:
        confirmed = EuroautoSearchFetcher._confirmed_product_image(images, article)
        return confirmed[0] if confirmed else ''

    @staticmethod
    def _confirmed_product_image(
        images: list[dict], article: str,
    ) -> tuple[str, dict] | None:
        target = _normalized_code(article)
        for image in images:
            page_url = str(image.get('url') or '').strip()
            title = str(image.get('title') or '')
            original_url = str((image.get('properties') or {}).get('url') or '').strip()
            image_match = re.search(
                r'https?://file\.euroauto\.ru/v2/file/parts/new/(?P<id>\d+)/\d+\.jpg',
                original_url,
                re.IGNORECASE,
            )
            if not image_match:
                continue
            page_path = urlparse(page_url).path
            title_matches = target in _normalized_code(title)
            firms_matches = '/firms/' in page_path and target in _normalized_code(page_path)
            direct_match = re.search(r'/part/new/(?P<id>\d+)', page_path)
            direct_matches = bool(
                direct_match
                and direct_match.group('id') == image_match.group('id')
                and title_matches
            )
            if firms_matches or direct_matches:
                return image_match.group('id'), image
        return None

    @staticmethod
    def _best_source_url(payload: dict, article: str) -> str:
        matches = []
        for result in payload.get('results') or []:
            url = str(result.get('url') or '').strip()
            rank = euroauto_result_rank(result, article)
            if not url or rank is None:
                continue
            matches.append((rank, url))
        return max(matches, default=((0,), ''))[1]


def _normalized_code(value: str) -> str:
    return re.sub(r'[^A-ZА-ЯЁ0-9]', '', str(value or '').upper())


def euroauto_result_rank(result: dict, article: str) -> tuple | None:
    """Prefer exact pages, then exact result blocks with applicability.

    Euroauto often exposes both a sparse ``/firms/<brand>/<article>`` result
    and a richer catalogue result for the same part. Search relevance alone
    can put the sparse page first, dropping fitments from enrichment. Full-page
    ``raw_content`` is deliberately excluded: it can contain neighbouring
    products and recommendation blocks unrelated to the requested article.
    """
    url = str(result.get('url') or '').strip()
    profile_text = ' '.join([
        str(result.get('title') or ''),
        str(result.get('content') or ''),
    ])
    target = _normalized_code(article)
    normalized_haystack = _normalized_code(profile_text)
    if not url or not target or target not in normalized_haystack:
        return None
    direct = int(bool(re.search(r'/part/new/\d+', url)))
    has_fitment = int(bool(re.search(
        r'\(\s*\d{4}(?:\s*[-–]\s*\d{4}|\s*>)\s*\)',
        profile_text,
    )))
    firm = int('/firms/' in url)
    catalog = int('/catalog/' in url)
    article_mentions = min(normalized_haystack.count(target), 10)
    try:
        relevance = float(result.get('score') or 0)
    except (TypeError, ValueError):
        relevance = 0
    return (
        direct,
        has_fitment,
        firm,
        catalog,
        article_mentions,
        min(len(profile_text), 20000),
        relevance,
    )


def get_part_fetcher(source_id: str, tenant=None):
    policy = get_part_source_policy(source_id)
    if policy.transport == 'httpx':
        return HttpxPartFetcher()
    if policy.transport == 'catalog_search':
        return EuroautoSearchFetcher(tenant=tenant)
    raise ValueError(f'Unsupported part parser transport: {policy.transport}')
