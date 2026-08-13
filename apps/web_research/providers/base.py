from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from apps.core.provider_boundary import (
    is_authoritative_provider_rejection,
    is_proven_pre_send_failure,
)
from apps.web_research.search_context import SearchContext


class WebSearchProviderError(RuntimeError):
    def __init__(
        self, message: str, *, retryable: bool = False, code: str = 'provider_error',
        outcome_uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.outcome_uncertain = outcome_uncertain
        # Filled by the durable accounting wrapper after an attempt exists.
        # Callers use it to acknowledge every consumed workflow slot exactly.
        self.attempt_id: int | None = None
        self.workflow_id: int | None = None


def provider_transport_error(
    provider_name: str,
    exc: BaseException,
) -> WebSearchProviderError:
    """Classify a Requests failure without persisting its potentially sensitive text."""
    if is_proven_pre_send_failure(exc):
        return WebSearchProviderError(
            f'{provider_name} request was not sent.',
            retryable=True,
            code='pre_send_failure',
        )
    return WebSearchProviderError(
        f'{provider_name} request outcome is uncertain; automatic retry is forbidden.',
        retryable=False,
        code='connection_error',
        outcome_uncertain=True,
    )


def provider_http_error(
    provider_name: str,
    status_code: int,
    *,
    documented_rejections: frozenset[int],
) -> WebSearchProviderError:
    """Classify a non-2xx response from a synchronous paid search API."""
    if is_authoritative_provider_rejection(
        status_code,
        documented_statuses=documented_rejections,
    ):
        return WebSearchProviderError(
            f'{provider_name} rejected the request (HTTP {status_code}).',
            retryable=False,
            code=f'http_{status_code}',
        )
    return WebSearchProviderError(
        f'{provider_name} request outcome is uncertain; automatic retry is forbidden.',
        retryable=False,
        code=f'http_{status_code}',
        outcome_uncertain=True,
    )


def provider_response_error(provider_name: str, detail: str = '') -> WebSearchProviderError:
    """Return a fail-closed error for an invalid response after a paid request."""
    suffix = f': {detail}' if detail else ''
    return WebSearchProviderError(
        f'{provider_name} returned an invalid response{suffix}.',
        retryable=False,
        code='invalid_response',
        outcome_uncertain=True,
    )


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str
    rank: int
    content: str = ''
    raw_content: str = ''
    score: float | None = None
    published_at: str = ''
    metadata: dict[str, Any] | None = None


class BaseWebSearchProvider(ABC):
    provider_id = ''
    display_name = ''

    def __init__(
        self,
        *,
        credentials: dict | None = None,
        parameters: dict | None = None,
    ) -> None:
        self.credentials = credentials or {}
        self.parameters = parameters or {}

    @abstractmethod
    def is_available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def search(
        self, query: str, *, count: int = 8, context: SearchContext | None = None,
    ) -> list[WebSearchResult]:
        raise NotImplementedError
