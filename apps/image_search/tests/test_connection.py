from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.db import DatabaseError

from apps.image_search.sources.brave import BraveImageSource
from apps.image_search.sources.connection import image_source_api_key


def test_env_key_is_used_only_after_successful_missing_row_lookup():
    queryset = MagicMock()
    queryset.first.return_value = None

    with patch(
        'apps.web_research.models.WebSearchConnection.objects.filter',
        return_value=queryset,
    ):
        assert image_source_api_key(
            'brave',
            None,
            'BRAVE_SEARCH_API_KEY',
            'environment-fallback',
        ) == 'environment-fallback'


def test_connection_database_error_propagates_before_paid_network_call():
    product = SimpleNamespace(
        pk=41,
        tenant_id=17,
        tenant=SimpleNamespace(pk=17),
        article='SAFE-41',
        brand='BRAND',
        name='Part',
    )
    source = BraveImageSource(product)

    with patch(
        'apps.web_research.models.WebSearchConnection.objects.filter',
        side_effect=DatabaseError('connection state unavailable'),
    ), patch('apps.image_search.sources.brave.requests.get') as network_call:
        with pytest.raises(DatabaseError, match='connection state unavailable'):
            source.search()

    network_call.assert_not_called()
