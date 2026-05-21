"""Тесты DuckDuckGoSource — все HTTP запросы замоканы."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from apps.image_search.sources.duckduckgo import DuckDuckGoSource


class FakeProduct:
    """Заглушка Product для тестирования без БД."""

    article = '25327H5010'
    brand = 'HYUNDAI-KIA'
    name = 'Фильтр воздушный'


def _make_source():
    return DuckDuckGoSource(FakeProduct())


def _build_mock_client(vqd_html: str, images_json: dict):
    """Создаёт мок httpx.Client для одного цикла vqd + images."""
    mock_vqd_resp = MagicMock()
    mock_vqd_resp.text = vqd_html

    mock_imgs_resp = MagicMock()
    mock_imgs_resp.json.return_value = images_json

    mock_client = MagicMock()
    mock_client.get.side_effect = [mock_vqd_resp, mock_imgs_resp]
    return mock_client


class TestDuckDuckGoSource:
    """Тесты DuckDuckGoSource."""

    def test_search_возвращает_кандидатов(self):
        images_json = {'results': [
            {'image': 'https://img1.example.com/a.jpg', 'width': 800, 'height': 600, 'title': 'Фильтр'},
            {'image': 'https://img2.example.com/b.jpg', 'width': 1200, 'height': 900, 'title': 'Фильтр 2'},
        ]}
        mock_client = _build_mock_client('vqd=3-abc123', images_json)

        with patch('apps.image_search.sources.duckduckgo.httpx.Client') as MockClient:
            MockClient.return_value.__enter__.return_value = mock_client
            results = _make_source().search()

        assert len(results) == 2
        assert results[0].url == 'https://img1.example.com/a.jpg'
        assert results[0].tier == 4
        assert results[0].source_id == 'duckduckgo'
        assert results[0].width == 800
        assert results[0].height == 600
        assert results[0].raw_meta['confidence'] == 'HIGH'

    def test_search_без_vqd_возвращает_пустой_список(self):
        mock_vqd_resp = MagicMock()
        mock_vqd_resp.text = 'страница без токена'
        mock_client = MagicMock()
        mock_client.get.return_value = mock_vqd_resp

        with patch('apps.image_search.sources.duckduckgo.httpx.Client') as MockClient:
            MockClient.return_value.__enter__.return_value = mock_client
            results = _make_source().search()

        assert results == []

    def test_search_дедуплицирует_url(self):
        dup_url = 'https://example.com/img.jpg'
        images_json = {'results': [
            {'image': dup_url, 'width': 800, 'height': 600, 'title': 'A'},
            {'image': dup_url, 'width': 800, 'height': 600, 'title': 'B'},
        ]}
        mock_client = _build_mock_client('vqd=3-xyz', images_json)

        with patch('apps.image_search.sources.duckduckgo.httpx.Client') as MockClient:
            MockClient.return_value.__enter__.return_value = mock_client
            results = _make_source().search()

        urls = [r.url for r in results]
        assert len(urls) == len(set(urls))

    def test_search_не_падает_при_http_ошибке(self):
        with patch('apps.image_search.sources.duckduckgo.httpx.Client') as MockClient:
            mock_client = MockClient.return_value.__enter__.return_value
            mock_client.get.side_effect = httpx.HTTPError('timeout')
            results = _make_source().search()

        assert results == []

    def test_search_пропускает_результаты_без_url(self):
        images_json = {'results': [
            {'image': '', 'width': 800, 'height': 600, 'title': 'Без URL'},
            {'image': 'https://valid.example.com/img.jpg', 'width': 800, 'height': 600, 'title': 'OK'},
        ]}
        mock_client = _build_mock_client('vqd=3-abc', images_json)

        with patch('apps.image_search.sources.duckduckgo.httpx.Client') as MockClient:
            MockClient.return_value.__enter__.return_value = mock_client
            results = _make_source().search()

        assert len(results) == 1
        assert results[0].url == 'https://valid.example.com/img.jpg'

    def test_source_зарегистрирован_в_реестре(self):
        # Источник должен быть зарегистрирован при импорте модуля через @register
        import apps.image_search.sources.duckduckgo  # noqa: F401
        from apps.image_search.sources.registry import get_registered_sources

        assert 'duckduckgo' in get_registered_sources()

    def test_source_id_и_tier(self):
        source = _make_source()
        assert source.source_id == 'duckduckgo'
        assert source.tier == 4
        assert source.is_free is True
        assert source.requires_key is False

    @pytest.mark.parametrize('vqd_text,expected', [
        ('vqd=3-abc123def', '3-abc123def'),
        ("vqd='3-xyz'", '3-xyz'),
        ('no token here', ''),
    ])
    def test_get_vqd_парсинг(self, vqd_text, expected):
        source = _make_source()
        mock_resp = MagicMock()
        mock_resp.text = vqd_text

        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp
        result = source._get_vqd(mock_client, 'test query')

        assert result == expected
