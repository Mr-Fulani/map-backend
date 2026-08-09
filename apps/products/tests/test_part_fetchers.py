import json
from unittest.mock import Mock, patch

import requests
import pytest

from apps.core.url_security import ResponseTooLarge, UnsafePublicURL
from apps.products.part_fetchers import (
    EuroautoSearchFetcher,
    HttpxPartFetcher,
    get_part_fetcher,
)
from apps.web_research.providers.base import WebSearchResult


def test_euroauto_fetcher_uses_managed_search_and_expands_confirmed_product_images():
    provider = Mock(provider_id='brave')
    provider.search.return_value = [
        WebSearchResult(
            url='https://rostov-na-donu.euroauto.ru/firms/metaco/8940289',
            title='8940-289 Metaco Фонарь задний наружный левый',
            snippet='Metaco HYUNDAI SOLARIS (2017>)',
            rank=1,
            score=0.91,
        ),
    ]
    provider.search_images.return_value = [{
        'url': 'https://rostov-na-donu.euroauto.ru/firms/metaco/8940289',
        'title': '8940-289 Metaco Фонарь задний наружный левый',
        'properties': {
            'url': (
                'https://file.euroauto.ru/v2/file/parts/new/6148741/'
                '1.jpg?thumbnail=308x244'
            ),
        },
    }]
    candidate = Mock(provider=provider)
    head_responses = [
        Mock(status_code=200),
        Mock(status_code=200),
        Mock(status_code=404),
    ]

    with patch(
        'apps.web_research.routing.search_provider_candidates',
        return_value=[candidate],
    ) as candidates, patch(
        'apps.products.part_fetchers.request_public_http_url',
        side_effect=head_responses,
    ) as head:
        page = EuroautoSearchFetcher(tenant='tenant').fetch(
            'https://euroauto.ru/search/?q=8940-289&hint=%D0%A4%D0%BE%D0%BD%D0%B0%D1%80%D1%8C'
        )

    candidates.assert_called_once_with('tenant')
    request = provider.search.call_args
    assert '8940-289' in request.args[0]
    provider.search_images.assert_called_once()
    payload = json.loads(page.html)
    assert payload['_map_product_images'] == [
        'https://file.euroauto.ru/v2/file/parts/new/6148741/1.jpg',
        'https://file.euroauto.ru/v2/file/parts/new/6148741/2.jpg',
    ]
    assert page.url.endswith('/firms/metaco/8940289')
    assert head.call_count == 3
    assert all(call.kwargs['method'] == 'HEAD' for call in head.call_args_list)
    assert all(call.kwargs['status_only'] is True for call in head.call_args_list)
    assert all(call.kwargs['redirect_policy'] == 'none' for call in head.call_args_list)


def test_part_fetcher_registry_builds_euroauto_transport_for_tenant():
    fetcher = get_part_fetcher('euroauto', tenant='tenant')

    assert isinstance(fetcher, EuroautoSearchFetcher)
    assert fetcher.tenant == 'tenant'


def _response(content: bytes, *, url='https://catalog.example/final'):
    response = requests.Response()
    response.status_code = 200
    response.url = url
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    response._content = content
    response._content_consumed = True
    response.encoding = 'utf-8'
    return response


def test_catalogue_fetcher_uses_central_bounded_transport(settings):
    settings.PART_PAGE_MAX_BYTES = 1234
    response = _response('<h1>Каталог</h1>'.encode())

    with patch(
        'apps.products.part_fetchers.request_public_http_url',
        return_value=response,
    ) as request:
        page = HttpxPartFetcher().fetch('https://catalog.example/start')

    request.assert_called_once_with(
        'https://catalog.example/start',
        timeout=(5, 20),
        headers={'User-Agent': HttpxPartFetcher.user_agent},
        max_response_bytes=1234,
    )
    assert page.html == '<h1>Каталог</h1>'
    assert page.url == 'https://catalog.example/final'


def test_catalogue_fetcher_propagates_unsafe_url():
    with patch(
        'apps.products.part_fetchers.request_public_http_url',
        side_effect=UnsafePublicURL('private redirect'),
    ), pytest.raises(UnsafePublicURL):
        HttpxPartFetcher().fetch('https://catalog.example/start')


def test_catalogue_fetcher_propagates_byte_budget_failure():
    with patch(
        'apps.products.part_fetchers.request_public_http_url',
        side_effect=ResponseTooLarge('large response'),
    ), pytest.raises(ResponseTooLarge):
        HttpxPartFetcher().fetch('https://catalog.example/product')


def test_euroauto_rejects_similar_image_without_exact_article_page_evidence():
    images = [{
        'url': 'https://tihvin.euroauto.ru/catalog/zadnie-fonari-1058',
        'title': 'Фонари задние Metaco купить в Тихвине',
        'properties': {
            'url': 'https://file.euroauto.ru/v2/file/parts/new/2781511/1.jpg',
        },
    }]

    assert EuroautoSearchFetcher._confirmed_product_id(images, '8940-289') == ''


def test_euroauto_prefers_catalogue_result_with_fitment_over_sparse_firm_page():
    payload = {
        'results': [
            {
                'url': 'https://euroauto.ru/firms/metaco/8940289',
                'title': '8940-289 Metaco Фонарь задний наружный левый',
                'content': '',
                'score': 0.99,
            },
            {
                'url': 'https://euroauto.ru/catalog/zadnie-fonari/proizvoditel-metaco',
                'title': 'Фонари задние Metaco',
                'content': (
                    'Фонарь задний наружный левый Metaco 8940-289. '
                    'HYUNDAI SOLARIS (2017>)'
                ),
                'score': 0.75,
            },
        ],
    }

    assert EuroautoSearchFetcher._best_source_url(payload, '8940-289') == (
        'https://euroauto.ru/catalog/zadnie-fonari/proizvoditel-metaco'
    )
