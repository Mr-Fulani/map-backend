import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
import pytest

from apps.core.url_security import ResponseTooLarge, UnsafePublicURL
from apps.products.part_fetchers import (
    build_euroauto_workflow_snapshot,
    EuroautoSearchFetcher,
    HttpxPartFetcher,
    get_part_fetcher,
)
from apps.web_research.providers.base import WebSearchProviderError, WebSearchResult
from apps.web_research.providers.brave import BraveWebSearchProvider


@pytest.fixture(autouse=True)
def isolate_paid_search_ledger(monkeypatch):
    """Pure fetcher tests exercise parsing; ledger integration has DB tests."""
    monkeypatch.setattr(
        'apps.web_research.accounting.execute_recorded_web_search',
        lambda **kwargs: kwargs['call'](),
    )
    monkeypatch.setattr(
        'apps.web_research.accounting.replay_recorded_web_search',
        lambda *args, **kwargs: None,
    )


def _workflow_fetcher(tenant, candidates, *, hint=''):
    """Bind pure adapter tests to the same immutable public plan as runtime."""
    fetcher = EuroautoSearchFetcher(tenant=tenant)
    with patch(
        'apps.web_research.routing.search_provider_candidates',
        return_value=candidates,
    ):
        snapshot = build_euroauto_workflow_snapshot(
            tenant,
            article='8940-289',
            hint=hint,
        )
    fetcher.set_web_search_workflow(SimpleNamespace(pk=1, input_snapshot=snapshot))
    by_provider = {
        candidate.provider.provider_id: candidate
        for candidate in candidates
    }
    return fetcher, patch.object(
        fetcher,
        '_resolve_planned_candidate',
        side_effect=lambda plan: by_provider[plan['provider_id']],
    )


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

    fetcher, resolve = _workflow_fetcher(
        'tenant',
        [candidate],
        hint='Фонарь',
    )
    with resolve, patch(
        'apps.products.part_fetchers.request_public_http_url',
        side_effect=head_responses,
    ) as head:
        page = fetcher.fetch(
            'https://euroauto.ru/search/?q=8940-289&hint=%D0%A4%D0%BE%D0%BD%D0%B0%D1%80%D1%8C'
        )

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


def test_euroauto_stops_before_second_paid_provider_on_uncertain_text_search():
    first = Mock(provider_id='brave')
    first.search.side_effect = WebSearchProviderError(
        'uncertain',
        code='outcome_uncertain',
        outcome_uncertain=True,
    )
    second = Mock(provider_id='tavily')

    fetcher, resolve = _workflow_fetcher(
        'tenant',
        [Mock(provider=first), Mock(provider=second)],
    )
    with resolve, pytest.raises(WebSearchProviderError) as error:
        fetcher.fetch(
            'https://euroauto.ru/search/?q=8940-289',
        )

    assert error.value.outcome_uncertain is True
    first.search.assert_called_once()
    second.search.assert_not_called()


def test_euroauto_does_not_hide_uncertain_paid_image_search():
    provider = Mock(provider_id='brave')
    provider.search.return_value = [WebSearchResult(
        url='https://euroauto.ru/firms/metaco/8940289',
        title='8940-289 Metaco',
        snippet='8940-289 Metaco',
        rank=1,
    )]
    provider.search_images.side_effect = WebSearchProviderError(
        'uncertain',
        code='outcome_uncertain',
        outcome_uncertain=True,
    )

    fetcher, resolve = _workflow_fetcher(
        'tenant',
        [Mock(provider=provider)],
    )
    with resolve, pytest.raises(WebSearchProviderError) as error:
        fetcher.fetch(
            'https://euroauto.ru/search/?q=8940-289',
        )

    assert error.value.outcome_uncertain is True


def test_euroauto_does_not_hide_existing_reconciliation_fence_on_images():
    provider = Mock(provider_id='brave')
    provider.search.return_value = [WebSearchResult(
        url='https://euroauto.ru/firms/metaco/8940289',
        title='8940-289 Metaco',
        snippet='8940-289 Metaco',
        rank=1,
    )]
    provider.search_images.side_effect = WebSearchProviderError(
        'reconciliation required',
        code='provider_reconciliation_required',
        outcome_uncertain=False,
    )

    fetcher, resolve = _workflow_fetcher(
        'tenant',
        [Mock(provider=provider)],
    )
    with resolve, pytest.raises(WebSearchProviderError) as error:
        fetcher.fetch(
            'https://euroauto.ru/search/?q=8940-289',
        )

    assert error.value.code == 'provider_reconciliation_required'
    assert error.value.outcome_uncertain is False


def test_euroauto_paid_request_uses_snapshotted_public_provider_parameters():
    provider = BraveWebSearchProvider(
        credentials={'api_key': 'test-key'},
        parameters={
            'country': 'de',
            'search_lang': 'de',
            'extra_snippets': False,
            'timeout': 11,
        },
    )
    candidate = SimpleNamespace(provider=provider, connection=None)
    fetcher, resolve = _workflow_fetcher('tenant', [candidate])

    # An admin edit after acquisition must not alter the public bytes of this
    # workflow's not-yet-started paid slot.
    provider.parameters = {
        'country': 'us',
        'search_lang': 'en',
        'extra_snippets': True,
        'timeout': 25,
    }
    rejected = Mock(status_code=401)
    with resolve, patch(
        'apps.web_research.providers.brave.bounded_http_request',
        return_value=rejected,
    ) as paid_http, pytest.raises(WebSearchProviderError):
        fetcher.fetch('https://euroauto.ru/search/?q=8940-289')

    request_params = paid_http.call_args.kwargs['params']
    assert request_params['country'] == 'de'
    assert request_params['search_lang'] == 'de'
    assert request_params['extra_snippets'] is False


def _response(content: bytes, *, url='https://catalog.example/final'):
    response = requests.Response()
    response.status_code = 200
    response.url = url
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    response._content = content
    response.__dict__['_content_consumed'] = True
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
