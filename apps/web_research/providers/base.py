from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from apps.web_research.search_context import SearchContext


class WebSearchProviderError(RuntimeError):
    def __init__(
        self, message: str, *, retryable: bool = False, code: str = 'provider_error',
    ):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


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

    def __init__(self, *, credentials: dict | None = None, parameters: dict | None = None):
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
