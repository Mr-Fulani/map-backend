from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from apps.image_search.sources.tavily import TavilyImageSource


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


def test_tavily_reports_rate_limit_for_pipeline_diagnostics():
    response = MagicMock(status_code=429)
    connection = SimpleNamespace(enabled=True, parameters={})
    source = TavilyImageSource(FakeProduct())
    with patch(
        'apps.image_search.sources.tavily.image_source_connection', return_value=connection,
    ), patch(
        'apps.image_search.sources.tavily.image_source_api_key', return_value='test-key',
    ), patch('apps.image_search.sources.tavily.requests.post', return_value=response):
        assert source.search() == []

    assert source.last_error_code == 'rate_limited'
