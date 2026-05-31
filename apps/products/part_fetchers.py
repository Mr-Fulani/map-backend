from dataclasses import dataclass

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


def get_part_fetcher(source_id: str):
    policy = get_part_source_policy(source_id)
    if policy.transport == 'httpx':
        return HttpxPartFetcher()
    raise ValueError(f'Unsupported part parser transport: {policy.transport}')
