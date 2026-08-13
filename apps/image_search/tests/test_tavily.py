from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from apps.core.http_responses import TrustedResponseDeadlineExceeded
from apps.image_search.sources.base import ImageSearchOutcomeUncertain
from apps.image_search.sources.tavily import TavilyImageSource


@pytest.fixture(autouse=True)
def _unit_image_provider_without_shared_ledger():
    """Adapter unit tests isolate HTTP parsing from DB accounting integration."""
    connection = SimpleNamespace(
        enabled=True,
        priority=100,
        parameters={},
        database_connection=None,
    )
    with patch(
        'apps.image_search.sources.tavily.execute_recorded_image_search',
        side_effect=lambda source, query, call, **kwargs: call(),
    ) as recorded_call, patch(
        'apps.image_search.sources.connection.image_source_connection',
        return_value=connection,
    ):
        yield recorded_call


class FakeProduct:
    article = '92402D4000'
    brand = 'Kia'
    name = 'Фонарь правый внешний Kia Optima JF'


def test_tavily_parses_described_images_and_deduplicates_urls():
    response = MagicMock(status_code=200)
    response.json.return_value = {
        'images': [
            {'url': 'https://img.example.com/lamp.jpg', 'description': 'Kia Optima lamp'},
            {'url': 'https://img.example.com/lamp.jpg', 'description': 'duplicate'},
            'https://img.example.com/second.jpg',
        ],
    }
    connection = SimpleNamespace(enabled=True, parameters={})
    with patch(
        'apps.image_search.sources.tavily.image_source_connection', return_value=connection,
    ), patch(
        'apps.image_search.sources.tavily.image_source_api_key', return_value='test-key',
    ), patch('apps.image_search.sources.tavily.requests.post', return_value=response):
        results = TavilyImageSource(FakeProduct()).search()

    assert [item.url for item in results] == [
        'https://img.example.com/lamp.jpg',
        'https://img.example.com/second.jpg',
    ]
    assert results[0].raw_meta['title'] == 'Kia Optima lamp'


def test_each_tavily_http_query_uses_shared_accounting_ledger(
    _unit_image_provider_without_shared_ledger,
):
    response = MagicMock(status_code=200)
    response.json.return_value = {'images': []}
    connection = SimpleNamespace(enabled=True, parameters={})
    source = TavilyImageSource(FakeProduct())
    source.max_queries = 1

    with patch(
        'apps.image_search.sources.tavily.image_source_connection',
        return_value=connection,
    ), patch(
        'apps.image_search.sources.tavily.image_source_api_key',
        return_value='test-key',
    ), patch(
        'apps.image_search.sources.tavily.bounded_http_request',
        return_value=response,
    ):
        source.search()

    _unit_image_provider_without_shared_ledger.assert_called_once()


def test_tavily_rate_limit_is_uncertain_and_stops_all_queries():
    response = MagicMock(status_code=429)
    connection = SimpleNamespace(enabled=True, parameters={})
    source = TavilyImageSource(FakeProduct())
    with patch(
        'apps.image_search.sources.tavily.image_source_connection', return_value=connection,
    ), patch(
        'apps.image_search.sources.tavily.image_source_api_key', return_value='test-key',
    ), patch(
        'apps.image_search.sources.tavily.requests.post', return_value=response,
    ) as post, pytest.raises(ImageSearchOutcomeUncertain):
        source.search()

    assert source.last_error_code == 'http_429'
    post.assert_called_once()


def test_tavily_transport_timeout_is_not_hidden_or_retried():
    connection = SimpleNamespace(enabled=True, parameters={})
    with patch(
        'apps.image_search.sources.tavily.image_source_connection',
        return_value=connection,
    ), patch(
        'apps.image_search.sources.tavily.image_source_api_key',
        return_value='test-key',
    ), patch(
        'apps.image_search.sources.tavily.requests.post',
        side_effect=requests.ReadTimeout('unknown outcome'),
    ) as post, pytest.raises(ImageSearchOutcomeUncertain):
        TavilyImageSource(FakeProduct()).search()

    post.assert_called_once()


def test_tavily_rejects_malformed_top_level_json():
    response = MagicMock(status_code=200)
    response.json.return_value = []
    connection = SimpleNamespace(enabled=True, parameters={})
    source = TavilyImageSource(FakeProduct())
    with patch(
        'apps.image_search.sources.tavily.image_source_connection', return_value=connection,
    ), patch(
        'apps.image_search.sources.tavily.image_source_api_key', return_value='test-key',
    ), patch(
        'apps.image_search.sources.tavily.requests.post', return_value=response,
    ) as post, pytest.raises(ImageSearchOutcomeUncertain):
        source.search()

    assert source.last_error_code == 'invalid_response'
    post.assert_called_once()


def test_tavily_trusted_response_deadline_is_uncertain():
    connection = SimpleNamespace(enabled=True, parameters={})
    source = TavilyImageSource(FakeProduct())
    with patch(
        'apps.image_search.sources.tavily.image_source_connection',
        return_value=connection,
    ), patch(
        'apps.image_search.sources.tavily.image_source_api_key',
        return_value='test-key',
    ), patch(
        'apps.image_search.sources.tavily.bounded_http_request',
        side_effect=TrustedResponseDeadlineExceeded('deadline'),
    ), pytest.raises(ImageSearchOutcomeUncertain):
        source.search()

    assert source.last_error_code == 'invalid_response'


def test_tavily_documented_400_allows_safe_fallback():
    response = MagicMock(status_code=400)
    connection = SimpleNamespace(enabled=True, parameters={})
    source = TavilyImageSource(FakeProduct())
    with patch(
        'apps.image_search.sources.tavily.image_source_connection', return_value=connection,
    ), patch(
        'apps.image_search.sources.tavily.image_source_api_key', return_value='test-key',
    ), patch('apps.image_search.sources.tavily.requests.post', return_value=response):
        assert source.search() == []

    assert source.last_error_code == 'http_400'


@pytest.mark.parametrize('status_code', [402, 409, 424, 451])
def test_tavily_undocumented_4xx_is_uncertain_and_stops_queries(status_code):
    response = MagicMock(status_code=status_code)
    connection = SimpleNamespace(enabled=True, parameters={})
    source = TavilyImageSource(FakeProduct())
    with patch(
        'apps.image_search.sources.tavily.image_source_connection',
        return_value=connection,
    ), patch(
        'apps.image_search.sources.tavily.image_source_api_key',
        return_value='test-key',
    ), patch(
        'apps.image_search.sources.tavily.requests.post', return_value=response,
    ) as post, pytest.raises(ImageSearchOutcomeUncertain):
        source.search()

    assert source.last_error_code == f'http_{status_code}'
    post.assert_called_once()


def test_tavily_materializes_at_most_requested_images_per_query():
    response = MagicMock(status_code=200)
    response.json.return_value = {
        'images': [
            f'https://img.example.com/{index}.jpg'
            for index in range(10)
        ],
    }
    connection = SimpleNamespace(enabled=True, parameters={})
    with patch(
        'apps.image_search.sources.tavily.image_source_connection', return_value=connection,
    ), patch(
        'apps.image_search.sources.tavily.image_source_api_key', return_value='test-key',
    ), patch('apps.image_search.sources.tavily.requests.post', return_value=response):
        results = TavilyImageSource(FakeProduct()).search()

    assert len(results) == 5
