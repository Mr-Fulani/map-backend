"""Registry and neutral provider selection for the media processing domain."""

from collections.abc import Iterable

from django.conf import settings

from apps.media_processing.providers.base import BaseMediaProvider, MediaOperation


class MediaProviderNotFound(LookupError):
    pass


class MediaProviderUnavailable(RuntimeError):
    pass


_REGISTRY: dict[str, type[BaseMediaProvider]] = {}


def register_media_provider(provider_class: type[BaseMediaProvider]):
    """Register an adapter without coupling callers to its implementation module."""
    provider_id = provider_class.provider_id.strip().lower()
    if not provider_id:
        raise ValueError('Media provider must define provider_id')
    if provider_id in _REGISTRY:
        raise ValueError(f'Media provider {provider_id!r} is already registered')
    _REGISTRY[provider_id] = provider_class
    return provider_class


def get_media_provider(provider_id: str) -> BaseMediaProvider:
    provider_class = _REGISTRY.get((provider_id or '').strip().lower())
    if provider_class is None:
        raise MediaProviderNotFound(f'Unknown media provider: {provider_id}')
    return provider_class()


def list_media_providers(*, configured_only: bool = False) -> list[BaseMediaProvider]:
    providers = [provider_class() for provider_class in _REGISTRY.values()]
    if configured_only:
        providers = [provider for provider in providers if provider.is_configured()]
    return providers


def select_media_provider(
    operations: Iterable[str | MediaOperation],
    *,
    preferred_provider_ids: Iterable[str] = (),
) -> BaseMediaProvider:
    """Choose by capabilities and configured priority; tariff policy can supply preferences."""
    required = {MediaOperation(operation) for operation in operations}
    preferred = [provider_id for provider_id in preferred_provider_ids if provider_id]
    default_priority = list(getattr(settings, 'MEDIA_PROVIDER_PRIORITY', []))
    registered = list(_REGISTRY)
    ordered_ids = list(dict.fromkeys([*preferred, *default_priority, *registered]))

    for provider_id in ordered_ids:
        try:
            provider = get_media_provider(provider_id)
        except MediaProviderNotFound:
            continue
        if provider.is_configured() and provider.supports(required):
            return provider

    operation_names = ', '.join(sorted(operation.value for operation in required))
    raise MediaProviderUnavailable(
        f'No configured media provider supports operations: {operation_names}',
    )


def clear_media_provider_registry() -> None:
    """Test helper; production code should only register adapters at import time."""
    _REGISTRY.clear()
