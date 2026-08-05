from abc import ABC, abstractmethod
from dataclasses import dataclass


class WebSearchProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str
    rank: int


class BaseWebSearchProvider(ABC):
    provider_id = ''

    @abstractmethod
    def is_available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def search(self, query: str, *, count: int = 8) -> list[WebSearchResult]:
        raise NotImplementedError
