from unittest.mock import Mock, patch

import pytest
import requests
from django.test import override_settings

from apps.web_research.providers.brave import BraveWebSearchProvider
from apps.web_research.providers.base import WebSearchProviderError
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


@override_settings(BRAVE_SEARCH_API_KEY='test-key')
def test_brave_materializes_at_most_requested_results():
    response = Mock(status_code=200)
    response.json.return_value = {
        'web': {
            'results': [
                {'title': str(index), 'url': f'https://example.com/{index}'}
                for index in range(10)
            ],
        },
    }
    with patch('apps.web_research.providers.brave.requests.get', return_value=response):
        results = BraveWebSearchProvider().search('query', count=2)

    assert len(results) == 2


@pytest.mark.parametrize(
    'payload',
    [
        [],
        {'web': []},
        {'web': {'results': {}}},
        {'web': {'results': [42]}},
        {'web': {'results': [{'url': 'https://example.com', 'extra_snippets': {}}]}},
    ],
)
@override_settings(BRAVE_SEARCH_API_KEY='test-key')
def test_brave_rejects_malformed_json_shapes(payload):
    response = Mock(status_code=200)
    response.json.return_value = payload

    with patch('apps.web_research.providers.brave.requests.get', return_value=response):
        with pytest.raises(WebSearchProviderError) as error:
            BraveWebSearchProvider().search('query')

    assert error.value.code == 'invalid_response'
    assert error.value.outcome_uncertain is True


@override_settings(BRAVE_SEARCH_API_KEY='test-key', TRUSTED_API_RESPONSE_MAX_BYTES=5)
def test_brave_rejects_oversized_response_without_retrying():
    response = Mock(status_code=200, headers={})
    response.iter_content.return_value = iter([b'1234', b'56'])

    with patch('apps.web_research.providers.brave.requests.get', return_value=response):
        with pytest.raises(WebSearchProviderError) as error:
            BraveWebSearchProvider().search('query')

    assert error.value.code == 'invalid_response'
    assert error.value.retryable is False
    assert error.value.outcome_uncertain is True
    response.close.assert_called_once_with()


@pytest.mark.parametrize(
    'status_code',
    [402, 408, 409, 424, 429, 451, 500, 502, 503],
)
@override_settings(BRAVE_SEARCH_API_KEY='test-key')
def test_brave_non_authoritative_http_outcome_is_uncertain(status_code):
    response = Mock(status_code=status_code)
    with patch('apps.web_research.providers.brave.requests.get', return_value=response):
        with pytest.raises(WebSearchProviderError) as error:
            BraveWebSearchProvider().search('query')

    assert error.value.code == f'http_{status_code}'
    assert error.value.outcome_uncertain is True
    assert error.value.retryable is False


@override_settings(BRAVE_SEARCH_API_KEY='test-key')
def test_brave_documented_422_allows_provider_fallback():
    response = Mock(status_code=422)
    with patch('apps.web_research.providers.brave.requests.get', return_value=response):
        with pytest.raises(WebSearchProviderError) as error:
            BraveWebSearchProvider().search('query')

    assert error.value.code == 'http_422'
    assert error.value.outcome_uncertain is False


@override_settings(BRAVE_SEARCH_API_KEY='test-key')
def test_brave_connect_timeout_is_proven_pre_send():
    with patch(
        'apps.web_research.providers.brave.requests.get',
        side_effect=requests.ConnectTimeout('not sent'),
    ), pytest.raises(WebSearchProviderError) as error:
        BraveWebSearchProvider().search('query')

    assert error.value.code == 'pre_send_failure'
    assert error.value.retryable is True
    assert error.value.outcome_uncertain is False


@override_settings(BRAVE_SEARCH_API_KEY='test-key')
def test_brave_ambiguous_connection_error_is_uncertain_and_scrubbed():
    with patch(
        'apps.web_research.providers.brave.requests.get',
        side_effect=requests.ReadTimeout(
            'https://api.example.test/?api_key=must-not-leak',
        ),
    ), pytest.raises(WebSearchProviderError) as error:
        BraveWebSearchProvider().search('query')

    assert error.value.code == 'connection_error'
    assert error.value.outcome_uncertain is True
    assert 'must-not-leak' not in str(error.value)
    assert 'api.example.test' not in str(error.value)


@override_settings(BRAVE_SEARCH_API_KEY='test-key')
def test_brave_image_search_retains_source_page_for_exact_product_validation():
    response = Mock(status_code=200, headers={})
    response.json.return_value = {
        'results': [{
            'title': '8940-289 Metaco Фонарь задний наружный левый',
            'url': 'https://euroauto.ru/part/new/6148741/',
            'properties': {
                'url': 'https://file.euroauto.ru/v2/file/parts/new/6148741/1.jpg',
            },
        }],
    }
    with patch(
        'apps.web_research.providers.brave.requests.get', return_value=response,
    ) as get, patch(
        'apps.image_search.sources.brave.BraveImageSource._track_quota',
    ) as track_quota:
        results = BraveWebSearchProvider().search_images('site:euroauto.ru "8940-289"')

    assert results[0]['url'] == 'https://euroauto.ru/part/new/6148741/'
    assert results[0]['properties']['url'].endswith('/6148741/1.jpg')
    assert get.call_args.kwargs['params']['spellcheck'] is False
    track_quota.assert_called_once_with(response)


@override_settings(BRAVE_SEARCH_API_KEY='test-key')
def test_brave_image_search_500_is_uncertain():
    response = Mock(status_code=500)
    with patch(
        'apps.web_research.providers.brave.requests.get', return_value=response,
    ), pytest.raises(WebSearchProviderError) as error:
        BraveWebSearchProvider().search_images('query')

    assert error.value.code == 'http_500'
    assert error.value.outcome_uncertain is True


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


@override_settings(TAVILY_API_KEY='test-tavily-key')
def test_tavily_payload_materializes_results_and_images_to_requested_count():
    response = Mock(status_code=200)
    response.json.return_value = {
        'results': [
            {'url': f'https://example.com/{index}'}
            for index in range(5)
        ],
        'images': [
            {'url': f'https://images.example.com/{index}.jpg'}
            for index in range(5)
        ],
    }

    with patch('apps.web_research.providers.tavily.requests.post', return_value=response):
        payload = TavilyWebSearchProvider().search_payload(
            'query', count=2, include_images=True,
        )

    assert len(payload['results']) == 2
    assert len(payload['images']) == 2


@pytest.mark.parametrize(
    'payload',
    [
        [],
        {'results': {}},
        {'results': [42]},
        {'results': [], 'images': {}},
        {'results': [], 'images': [42]},
    ],
)
@override_settings(TAVILY_API_KEY='test-tavily-key')
def test_tavily_rejects_malformed_json_shapes(payload):
    response = Mock(status_code=200)
    response.json.return_value = payload

    with patch('apps.web_research.providers.tavily.requests.post', return_value=response):
        with pytest.raises(WebSearchProviderError) as error:
            TavilyWebSearchProvider().search_payload('query', include_images=True)

    assert error.value.code == 'invalid_response'
    assert error.value.outcome_uncertain is True


@pytest.mark.parametrize(
    'status_code',
    [402, 408, 409, 424, 429, 432, 433, 451, 500, 502, 503],
)
@override_settings(TAVILY_API_KEY='test-tavily-key')
def test_tavily_non_authoritative_http_outcome_is_uncertain(status_code):
    response = Mock(status_code=status_code)
    with patch('apps.web_research.providers.tavily.requests.post', return_value=response):
        with pytest.raises(WebSearchProviderError) as error:
            TavilyWebSearchProvider().search_payload('query')

    assert error.value.code == f'http_{status_code}'
    assert error.value.outcome_uncertain is True
    assert error.value.retryable is False


@override_settings(TAVILY_API_KEY='test-tavily-key')
def test_tavily_search_payload_supports_domain_limited_catalog_images():
    response = Mock(status_code=200)
    response.json.return_value = {
        'results': [],
        'images': [{'url': 'https://file.euroauto.ru/part.jpg'}],
    }
    with patch('apps.web_research.providers.tavily.requests.post', return_value=response) as post:
        payload = TavilyWebSearchProvider().search_payload(
            '8940-289 Metaco',
            include_domains=['euroauto.ru'],
            include_images=True,
            include_image_descriptions=True,
        )

    assert payload['images'][0]['url'].startswith('https://file.euroauto.ru/')
    request_data = post.call_args.kwargs['json']
    assert request_data['include_domains'] == ['euroauto.ru']
    assert request_data['include_images'] is True
    assert request_data['include_image_descriptions'] is True


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
