"""Stable capability-based contract for external media processing providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any


class MediaOperation(StrEnum):
    """Operations understood by MAP independently of a concrete provider API."""

    VALIDATE = 'validate'
    NORMALIZE = 'normalize'
    RESIZE = 'resize'
    REMOVE_BACKGROUND = 'remove_background'
    REPLACE_BACKGROUND = 'replace_background'
    ENHANCE = 'enhance'
    UPSCALE = 'upscale'
    ADD_SHADOW = 'add_shadow'
    GENERATIVE_FILL = 'generative_fill'


class MediaProviderResultStatus(StrEnum):
    SUCCEEDED = 'succeeded'
    PENDING = 'pending'
    FAILED = 'failed'


@dataclass(frozen=True)
class MediaProviderRequest:
    """Normalized request passed from business logic to any provider adapter."""

    input_url: str
    operations: tuple[MediaOperation, ...]
    parameters: dict[str, Any] = field(default_factory=dict)
    callback_url: str = ''
    idempotency_key: str = ''


@dataclass(frozen=True)
class MediaProviderResult:
    """Normalized synchronous or asynchronous provider response."""

    status: MediaProviderResultStatus
    provider_job_id: str = ''
    output_url: str = ''
    output_bytes: bytes | None = None
    output_content_type: str = ''
    metadata: dict[str, Any] = field(default_factory=dict)
    actual_cost: Decimal | None = None
    error_code: str = ''
    error_message: str = ''


class BaseMediaProvider(ABC):
    """Adapter implemented once for each paid, free, or self-hosted provider."""

    provider_id: str = ''
    display_name: str = ''
    supported_operations: frozenset[MediaOperation] = frozenset()

    def is_configured(self) -> bool:
        """Whether credentials and required settings are available."""
        return True

    def supports(self, operations: set[MediaOperation]) -> bool:
        return operations.issubset(self.supported_operations)

    def estimate_cost(
        self,
        operations: tuple[MediaOperation, ...],
        parameters: dict[str, Any] | None = None,
    ) -> Decimal | None:
        """Optional provider-native estimate; None means the provider cannot estimate."""
        return None

    @abstractmethod
    def process(self, request: MediaProviderRequest) -> MediaProviderResult:
        """Submit processing. May return either a final result or a pending job."""
        raise NotImplementedError

    def retrieve(self, provider_job_id: str) -> MediaProviderResult:
        """Poll an async provider. Synchronous adapters do not have to implement it."""
        raise NotImplementedError(f'{self.provider_id} does not support polling')

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        """Validate provider webhook authenticity before accepting its result."""
        return False
