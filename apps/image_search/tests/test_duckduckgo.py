"""Тесты DuckDuckGoSource — все вызовы DDGS замоканы."""

from unittest.mock import MagicMock, patch

from apps.image_search.sources.duckduckgo import DuckDuckGoSource


class FakeProduct:
    """Заглушка Product для тестирования без БД."""

    article = '25327H5010'
    brand = 'HYUNDAI-KIA'
    name = 'Фильтр воздушный'


def _make_source():
    return DuckDuckGoSource(FakeProduct())


def _patch_ddgs(results: list[dict]):
    """Патчит DDGS так, чтобы images() возвращал заданный список."""
    mock_ddgs = MagicMock()
    mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
    mock_ddgs.__exit__ = MagicMock(return_value=False)
    mock_ddgs.images.return_value = results
    return patch('apps.image_search.sources.duckduckgo.DDGS', return_value=mock_ddgs), mock_ddgs


class TestDuckDuckGoSource:
    """Тесты DuckDuckGoSource."""

    def test_search_возвращает_кандидатов(self):
        ddg_results = [
            {'image': 'https://img1.example.com/a.jpg', 'width': 800, 'height': 600, 'title': 'Фильтр'},
            {'image': 'https://img2.example.com/b.jpg', 'width': 1200, 'height': 900, 'title': 'Фильтр 2'},
        ]
        patcher, _ = _patch_ddgs(ddg_results)

        with patcher:
            results = _make_source().search()

        assert len(results) == 2
        assert results[0].url == 'https://img1.example.com/a.jpg'
        assert results[0].tier == 4
        assert results[0].source_id == 'duckduckgo'
        assert results[0].width == 800
        assert results[0].height == 600
        assert results[0].raw_meta['confidence'] == 'HIGH'

    def test_search_дедуплицирует_url(self):
        dup_url = 'https://example.com/img.jpg'
        ddg_results = [
            {'image': dup_url, 'width': 800, 'height': 600, 'title': 'A'},
            {'image': dup_url, 'width': 800, 'height': 600, 'title': 'B'},
        ]
        patcher, _ = _patch_ddgs(ddg_results)

        with patcher:
            results = _make_source().search()

        urls = [r.url for r in results]
        assert len(urls) == len(set(urls))

    def test_search_пропускает_результаты_без_url(self):
        ddg_results = [
            {'image': '', 'width': 800, 'height': 600, 'title': 'Без URL'},
            {'image': 'https://valid.example.com/img.jpg', 'width': 800, 'height': 600, 'title': 'OK'},
        ]
        patcher, _ = _patch_ddgs(ddg_results)

        with patcher:
            results = _make_source().search()

        assert len(results) == 1
        assert results[0].url == 'https://valid.example.com/img.jpg'

    def test_search_не_падает_при_ошибке_ddgs(self):
        mock_ddgs = MagicMock()
        mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs.images.side_effect = Exception('rate limit')

        with patch('apps.image_search.sources.duckduckgo.DDGS', return_value=mock_ddgs):
            results = _make_source().search()

        assert results == []

    def test_search_возвращает_пустой_список_при_нет_результатов(self):
        patcher, _ = _patch_ddgs([])

        with patcher:
            results = _make_source().search()

        assert results == []

    def test_source_зарегистрирован_в_реестре(self):
        import apps.image_search.sources.duckduckgo  # noqa: F401
        from apps.image_search.sources.registry import get_registered_sources

        assert 'duckduckgo' in get_registered_sources()

    def test_source_id_и_tier(self):
        source = _make_source()
        assert source.source_id == 'duckduckgo'
        assert source.tier == 4
        assert source.is_free is True
        assert source.requires_key is False
