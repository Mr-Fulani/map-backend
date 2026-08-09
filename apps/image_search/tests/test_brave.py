"""Тесты BraveImageSource — все HTTP-запросы и DB-счётчики замоканы."""

from unittest.mock import MagicMock, patch

from apps.image_search.sources.brave import BraveImageSource

# Патч _track_quota для всех тестов, которые не проверяют квоту напрямую
_patch_quota = patch('apps.image_search.sources.brave.BraveImageSource._track_quota')


class FakeProduct:
    """Заглушка Product для тестирования без БД."""

    article = 'C1016'
    brand = 'SAKURA'
    name = 'Фильтр масляный'


def _make_source():
    return BraveImageSource(FakeProduct())


def _brave_result(url='https://img.example.com/a.jpg', width=800, height=600):
    return {
        'title': 'Фильтр',
        'properties': {'url': url, 'width': width, 'height': height},
        'thumbnail': {'src': 'https://thumb.example.com/a.jpg', 'width': 200, 'height': 150},
    }


class TestBraveImageSource:
    """Тесты BraveImageSource."""

    def test_is_available_без_ключа(self):
        with patch('apps.image_search.sources.brave.settings') as s:
            s.BRAVE_SEARCH_API_KEY = ''
            assert _make_source().is_available() is False

    def test_is_available_с_ключом(self):
        with patch('apps.image_search.sources.brave.settings') as s, \
             patch('apps.image_search.sources.brave.BraveQuota') as mock_quota:
            s.BRAVE_SEARCH_API_KEY = 'test-key'
            mock_quota.is_soft_cap_reached.return_value = False
            assert _make_source().is_available() is True

    def test_is_available_при_достижении_soft_cap(self):
        with patch('apps.image_search.sources.brave.settings') as s, \
             patch('apps.image_search.sources.brave.BraveQuota') as mock_quota:
            s.BRAVE_SEARCH_API_KEY = 'test-key'
            mock_quota.is_soft_cap_reached.return_value = True
            assert _make_source().is_available() is False

    def test_search_возвращает_кандидатов(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'results': [_brave_result(), _brave_result('https://img2.example.com/b.jpg')]}

        with patch('apps.image_search.sources.brave.settings') as s, \
             patch('apps.image_search.sources.brave.requests.get', return_value=mock_resp), \
             _patch_quota:
            s.BRAVE_SEARCH_API_KEY = 'test-key'
            results = _make_source().search()

        assert len(results) == 2
        assert results[0].url == 'https://img.example.com/a.jpg'
        assert results[0].source_id == 'brave'
        assert results[0].tier == 3
        assert results[0].width == 800
        assert results[0].height == 600

    def test_search_дедуплицирует_url(self):
        dup_url = 'https://img.example.com/same.jpg'
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'results': [_brave_result(dup_url), _brave_result(dup_url)]}

        with patch('apps.image_search.sources.brave.settings') as s, \
             patch('apps.image_search.sources.brave.requests.get', return_value=mock_resp), \
             _patch_quota:
            s.BRAVE_SEARCH_API_KEY = 'test-key'
            results = _make_source().search()

        urls = [r.url for r in results]
        assert len(urls) == len(set(urls))

    def test_search_пропускает_результаты_без_url(self):
        no_url = {'title': 'Без URL', 'properties': {}, 'thumbnail': {}}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'results': [no_url, _brave_result()]}

        with patch('apps.image_search.sources.brave.settings') as s, \
             patch('apps.image_search.sources.brave.requests.get', return_value=mock_resp), \
             _patch_quota:
            s.BRAVE_SEARCH_API_KEY = 'test-key'
            results = _make_source().search()

        assert len(results) == 1

    def test_search_возвращает_пустой_список_при_401(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401

        with patch('apps.image_search.sources.brave.settings') as s, \
             patch('apps.image_search.sources.brave.requests.get', return_value=mock_resp):
            s.BRAVE_SEARCH_API_KEY = 'bad-key'
            results = _make_source().search()

        assert results == []

    def test_search_возвращает_пустой_список_при_сетевой_ошибке(self):
        with patch('apps.image_search.sources.brave.settings') as s, \
             patch('apps.image_search.sources.brave.requests.get', side_effect=Exception('timeout')):
            s.BRAVE_SEARCH_API_KEY = 'test-key'
            results = _make_source().search()

        assert results == []

    def test_search_rejects_malformed_nested_json(self):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            'results': [{'properties': [], 'thumbnail': {}}],
        }
        source = _make_source()

        with patch(
            'apps.image_search.sources.brave.image_source_api_key', return_value='test-key',
        ), patch(
            'apps.image_search.sources.brave.requests.get', return_value=mock_resp,
        ), _patch_quota:
            results = source.search()

        assert results == []
        assert source.last_error_code == 'invalid_response'

    def test_search_materializes_at_most_requested_results_per_query(self):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            'results': [
                _brave_result(f'https://img.example.com/{index}.jpg')
                for index in range(30)
            ],
        }
        source = _make_source()

        with patch(
            'apps.image_search.sources.brave.image_source_api_key', return_value='test-key',
        ), patch(
            'apps.image_search.sources.brave.requests.get', return_value=mock_resp,
        ), _patch_quota:
            results = source.search()

        assert len(results) == 15

    def test_source_зарегистрирован_в_реестре(self):
        import apps.image_search.sources.brave  # noqa: F401
        from apps.image_search.sources.registry import get_registered_sources
        assert 'brave' in get_registered_sources()

    def test_source_id_и_tier(self):
        source = _make_source()
        assert source.source_id == 'brave'
        assert source.tier == 3
        assert source.is_free is False
        assert source.requires_key is True
