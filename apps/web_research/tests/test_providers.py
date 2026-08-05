from unittest.mock import Mock, patch

from django.test import override_settings

from apps.web_research.providers.brave import BraveWebSearchProvider


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
