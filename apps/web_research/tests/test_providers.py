from unittest.mock import Mock, patch

import pytest
from django.test import override_settings

from apps.web_research.providers.brave import BraveWebSearchProvider
from apps.web_research.providers.tavily import TavilyWebSearchProvider
from apps.web_research.models import WebSearchConnection
from apps.tenants.services import TenantService
from apps.web_research.routing import search_provider_candidates


@override_settings(BRAVE_SEARCH_API_KEY='test-key')
def test_brave_web_search_parses_grounding_results():
    response = Mock(status_code=200)
    response.json.return_value = {
        'web': {
            'results': [{
                'title': '<strong>Kia Optima</strong> фонарь',
                'url': 'https://parts.example.com/kia-optima-light',
                'description': 'Фонарь <strong>правый</strong> для Kia Optima JF',
            }],
        },
    }
    with patch('apps.web_research.providers.brave.requests.get', return_value=response) as get:
        results = BraveWebSearchProvider().search('Kia Optima фонарь')

    assert len(results) == 1
    assert results[0].title == 'Kia Optima фонарь'
    assert results[0].snippet == 'Фонарь правый для Kia Optima JF'
    assert results[0].rank == 1
    assert get.call_args.kwargs['params']['q'] == 'Kia Optima фонарь'


@override_settings(BRAVE_SEARCH_API_KEY='')
def test_brave_web_search_is_unavailable_without_key():
    assert BraveWebSearchProvider().is_available() is False


@override_settings(TAVILY_API_KEY='test-tavily-key')
def test_tavily_parses_cleaned_content_and_score():
    response = Mock(status_code=200)
    response.json.return_value = {
        'results': [{
            'title': 'Kia Optima lamp',
            'url': 'https://parts.example.com/lamp',
            'content': 'Краткое описание',
            'raw_content': '<p>OEM 92402D4000 для Kia Optima JF</p>',
            'score': 0.91,
        }],
    }
    with patch('apps.web_research.providers.tavily.requests.post', return_value=response) as post:
        results = TavilyWebSearchProvider().search('Kia lamp')

    assert results[0].content == 'OEM 92402D4000 для Kia Optima JF'
    assert results[0].score == 0.91
    assert post.call_args.kwargs['json']['include_raw_content'] is True


@override_settings(TAVILY_API_KEY='')
def test_tavily_can_use_encrypted_connection_credentials():
    provider = TavilyWebSearchProvider(credentials={'api_key': 'connection-key'})
    assert provider.is_available() is True


def test_connection_credentials_are_encrypted_in_database(db):
    connection = WebSearchConnection(
        provider_id='tavily', display_name='Tavily',
    )
    connection.set_credentials({'api_key': 'plain-secret-value'})
    connection.save()

    stored = WebSearchConnection.objects.get(pk=connection.pk)
    assert b'plain-secret-value' not in bytes(stored.credentials_enc)
    assert stored.get_credentials() == {'api_key': 'plain-secret-value'}


@pytest.mark.django_db
@override_settings(BRAVE_SEARCH_API_KEY='legacy-server-key', TAVILY_API_KEY='')
def test_disabled_managed_connection_suppresses_legacy_environment_fallback():
    tenant, _ = TenantService.create_tenant(
        'provider-disabled', 'provider-disabled',
        'provider-disabled@test.com', 'pass12345',
    )
    WebSearchConnection.objects.create(
        provider_id='brave', display_name='Brave Search', is_active=False,
    )

    assert search_provider_candidates(tenant) == []
