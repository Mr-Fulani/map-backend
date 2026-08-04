from decimal import Decimal

import pytest
from django.test import override_settings

from apps.media_processing.providers.base import (
    BaseMediaProvider,
    MediaOperation,
    MediaProviderRequest,
    MediaProviderResult,
    MediaProviderResultStatus,
)
from apps.media_processing.providers.registry import (
    MediaProviderUnavailable,
    clear_media_provider_registry,
    register_media_provider,
    select_media_provider,
)


class ResizeProvider(BaseMediaProvider):
    provider_id = 'resize-provider'
    display_name = 'Resize provider'
    supported_operations = frozenset({MediaOperation.RESIZE})

    def process(self, request: MediaProviderRequest) -> MediaProviderResult:
        return MediaProviderResult(status=MediaProviderResultStatus.SUCCEEDED)


class FullProvider(BaseMediaProvider):
    provider_id = 'full-provider'
    display_name = 'Full provider'
    supported_operations = frozenset({
        MediaOperation.RESIZE,
        MediaOperation.REMOVE_BACKGROUND,
    })

    def estimate_cost(self, operations, parameters=None):
        return Decimal('2.5')

    def process(self, request: MediaProviderRequest) -> MediaProviderResult:
        return MediaProviderResult(status=MediaProviderResultStatus.SUCCEEDED)


@pytest.fixture(autouse=True)
def isolated_registry():
    clear_media_provider_registry()
    yield
    clear_media_provider_registry()


@override_settings(MEDIA_PROVIDER_PRIORITY=['resize-provider', 'full-provider'])
def test_router_skips_provider_without_required_capabilities():
    register_media_provider(ResizeProvider)
    register_media_provider(FullProvider)

    provider = select_media_provider([
        MediaOperation.RESIZE,
        MediaOperation.REMOVE_BACKGROUND,
    ])

    assert provider.provider_id == 'full-provider'


def test_router_reports_when_no_provider_supports_operations():
    register_media_provider(ResizeProvider)

    with pytest.raises(MediaProviderUnavailable):
        select_media_provider([MediaOperation.UPSCALE])


def test_duplicate_provider_registration_is_rejected():
    register_media_provider(ResizeProvider)

    with pytest.raises(ValueError, match='already registered'):
        register_media_provider(ResizeProvider)
