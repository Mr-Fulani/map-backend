import json
from unittest.mock import Mock, patch

from apps.products.part_fetchers import EuroautoSearchFetcher, get_part_fetcher
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
    http_client = Mock()
    http_client.head.side_effect = [
        Mock(status_code=200),
        Mock(status_code=200),
        Mock(status_code=404),
    ]
    http_client.__enter__ = Mock(return_value=http_client)
    http_client.__exit__ = Mock(return_value=False)

    with patch(
        'apps.web_research.routing.search_provider_candidates',
        return_value=[candidate],
    ) as candidates, patch(
        'apps.products.part_fetchers.httpx.Client', return_value=http_client,
    ):
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


def test_part_fetcher_registry_builds_euroauto_transport_for_tenant():
    fetcher = get_part_fetcher('euroauto', tenant='tenant')

    assert isinstance(fetcher, EuroautoSearchFetcher)
    assert fetcher.tenant == 'tenant'


def test_euroauto_rejects_similar_image_without_exact_article_page_evidence():
    images = [{
        'url': 'https://tihvin.euroauto.ru/catalog/zadnie-fonari-1058',
        'title': 'Фонари задние Metaco купить в Тихвине',
        'properties': {
            'url': 'https://file.euroauto.ru/v2/file/parts/new/2781511/1.jpg',
        },
    }]

    assert EuroautoSearchFetcher._confirmed_product_id(images, '8940-289') == ''
