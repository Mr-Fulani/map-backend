import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx

from apps.products.source_policy import get_part_source_policy


@dataclass(frozen=True)
class FetchedPage:
    html: str
    url: str
    status_code: int
    response: httpx.Response | None = None

    def raise_for_status(self) -> None:
        if self.response is not None:
            self.response.raise_for_status()
            return
        if self.status_code >= 400:
            request = httpx.Request('GET', self.url)
            response = httpx.Response(self.status_code, request=request)
            response.raise_for_status()


class HttpxPartFetcher:
    """Default HTTP transport for platform parser sources."""

    user_agent = 'MAP enrichment bot (+https://map.local)'

    def fetch(self, url: str) -> FetchedPage:
        response = httpx.get(
            url,
            timeout=20,
            follow_redirects=True,
            headers={'User-Agent': self.user_agent},
        )
        return FetchedPage(
            html=response.text,
            url=str(response.url),
            status_code=response.status_code,
            response=response,
        )


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

    def fetch(self, url: str) -> FetchedPage:
        from apps.web_research.routing import search_provider_candidates

        params = parse_qs(urlparse(url).query)
        article = (params.get('q') or [''])[0].strip()
        hint = (params.get('hint') or [''])[0].strip()
        if not article:
            raise ValueError('Euroauto search requires an article.')

        candidates = search_provider_candidates(self.tenant)
        if not candidates:
            raise RuntimeError(
                'Для каталога Euroauto требуется активное подключение Brave или Tavily.'
            )
        query = ' '.join(filter(None, [
            f'site:{self.domain}', f'"{article}"', hint, 'автозапчасть',
        ]))
        payload = {'results': [], 'images': []}
        last_error = None
        for candidate in candidates:
            provider = candidate.provider
            try:
                if provider.provider_id == 'tavily' and hasattr(provider, 'search_payload'):
                    provider_payload = provider.search_payload(
                        query,
                        count=10,
                        include_domains=[self.domain],
                    )
                    payload['results'].extend(provider_payload.get('results') or [])
                else:
                    payload['results'].extend([
                        {
                            'url': result.url,
                            'title': result.title,
                            'content': ' '.join(filter(None, [
                                result.snippet,
                                result.content,
                            ])),
                            'score': result.score,
                            'provider_id': provider.provider_id,
                        }
                        for result in provider.search(query, count=10)
                    ])
            except Exception as exc:
                last_error = exc
                continue
            if self._best_source_url(payload, article):
                break

        if not self._best_source_url(payload, article) and last_error:
            raise last_error

        brave = next(
            (
                candidate.provider
                for candidate in candidates
                if candidate.provider.provider_id == 'brave'
                and hasattr(candidate.provider, 'search_images')
            ),
            None,
        )
        if brave is not None:
            try:
                payload['images'] = brave.search_images(query, count=50)
            except Exception:
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
        with httpx.Client(
            timeout=8,
            follow_redirects=True,
            headers={'User-Agent': HttpxPartFetcher.user_agent},
        ) as client:
            for number in range(1, 11):
                candidate = f'{prefix}/{number}.jpg'
                try:
                    response = client.head(candidate)
                except httpx.HTTPError:
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
    """Prefer exact pages, then indexed evidence that contains applicability.

    Euroauto often exposes both a sparse ``/firms/<brand>/<article>`` result
    and a richer catalogue result for the same part. Search relevance alone
    can put the sparse page first, dropping fitments from enrichment.
    """
    url = str(result.get('url') or '').strip()
    haystack = ' '.join([
        str(result.get('title') or ''),
        str(result.get('content') or ''),
        str(result.get('raw_content') or ''),
    ])
    target = _normalized_code(article)
    normalized_haystack = _normalized_code(haystack)
    if not url or not target or target not in normalized_haystack:
        return None
    direct = int(bool(re.search(r'/part/new/\d+', url)))
    has_fitment = int(bool(re.search(
        r'\(\s*\d{4}(?:\s*[-–]\s*\d{4}|\s*>)\s*\)',
        haystack,
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
        min(len(haystack), 20000),
        relevance,
    )


def get_part_fetcher(source_id: str, tenant=None):
    policy = get_part_source_policy(source_id)
    if policy.transport == 'httpx':
        return HttpxPartFetcher()
    if policy.transport == 'catalog_search':
        return EuroautoSearchFetcher(tenant=tenant)
    raise ValueError(f'Unsupported part parser transport: {policy.transport}')
