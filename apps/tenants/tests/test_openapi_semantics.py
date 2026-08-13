import pytest
from drf_spectacular.generators import SchemaGenerator


@pytest.fixture
def seed_billing_plans():
    """Schema generation does not need the apps-wide database seed fixture."""


@pytest.fixture(scope='module')
def api_schema():
    return SchemaGenerator().get_schema(public=True)


def _resolve(api_schema, node):
    while '$ref' in node:
        component_name = node['$ref'].rsplit('/', 1)[-1]
        node = api_schema['components']['schemas'][component_name]
    return node


def _success_schema(api_schema, path_value, method='get'):
    operation = api_schema['paths'][path_value][method]
    response = operation['responses']['200']['content']['application/json']['schema']
    return _resolve(api_schema, response)


def test_jwt_login_schema_matches_response_payload(api_schema):
    operation = api_schema['paths']['/api/v1/auth/token/']['post']
    request_ref = operation['requestBody']['content'][
        'application/json'
    ]['schema']['$ref']
    response_ref = operation['responses']['200']['content'][
        'application/json'
    ]['schema']['$ref']
    response_name = response_ref.rsplit('/', 1)[-1]

    assert operation['tags'] == ['Auth']
    assert operation['summary'] == 'Войти по email и паролю'
    assert request_ref.endswith('/TenantTokenObtainPair')
    assert set(api_schema['components']['schemas'][response_name]['properties']) == {
        'access', 'refresh', 'browser_session_id', 'tenant', 'role', 'user',
    }

    refresh = api_schema['paths']['/api/v1/auth/token/refresh/']['post']
    assert refresh['tags'] == ['Auth']
    assert refresh['summary'] == 'Обновить JWT access-токен'


def test_browser_auth_schema_documents_csrf_and_signed_out_contracts(api_schema):
    csrf = api_schema['paths']['/api/v1/auth/browser/csrf/']['get']
    csrf_response = _resolve(
        api_schema,
        csrf['responses']['200']['content']['application/json']['schema'],
    )
    assert set(csrf_response['properties']) == {'status', 'csrf_token'}

    refresh = api_schema['paths']['/api/v1/auth/browser/refresh/']['post']
    assert {'200', '401', '403'} <= set(refresh['responses'])
    unauthorized = _resolve(
        api_schema,
        refresh['responses']['401']['content']['application/json']['schema'],
    )
    csrf_failed = _resolve(
        api_schema,
        refresh['responses']['403']['content']['application/json']['schema'],
    )
    assert set(unauthorized['properties']) == {'status', 'code', 'message'}
    assert set(csrf_failed['properties']) == {'status', 'code', 'message'}

    description = api_schema['info']['description']
    assert '/api/v1/auth/browser/csrf/' in description
    assert 'X-CSRFToken' in description
    assert '401 unauthorized' in description
    assert '403 csrf_failed' in description


def test_checkout_schema_documents_real_error_statuses_and_retry_header(api_schema):
    for path_value in (
        '/api/v1/billing/checkout/',
        '/api/v1/billing/ai-topup/',
    ):
        operation = api_schema['paths'][path_value]['post']
        assert {'200', '404', '409', '503'} <= set(operation['responses'])
        for status_code in ('404', '409', '503'):
            error_schema = _resolve(
                api_schema,
                operation['responses'][status_code]['content'][
                    'application/json'
                ]['schema'],
            )
            assert {'status', 'code'} <= set(error_schema['properties'])

        retry_after = operation['responses']['503']['headers']['Retry-After']
        assert retry_after['schema']['type'] == 'integer'
        assert 'checkout_pending' in retry_after['description']


def test_public_plans_and_operation_tags_are_explicit(api_schema):
    plans = api_schema['paths']['/api/v1/billing/plans/']['get']
    assert {} in plans['security']

    for path_value, path_item in api_schema['paths'].items():
        if not path_value.startswith('/api/v1/datasources/'):
            continue
        for method, operation in path_item.items():
            if method in {'get', 'post', 'put', 'patch', 'delete'}:
                assert operation['tags'] == ['Data sources']

    logs = api_schema['paths']['/api/v1/logs/']['get']
    assert logs['tags'] == ['Logs']

    declared_tags = {item['name'] for item in api_schema['tags']}
    used_tags = {
        tag
        for path_item in api_schema['paths'].values()
        for operation in path_item.values()
        if isinstance(operation, dict)
        for tag in operation.get('tags', [])
    }
    assert used_tags <= declared_tags
    assert 'v1' not in used_tags


def test_conditional_and_nullable_response_fields_are_accurate(api_schema):
    image_response = _success_schema(
        api_schema,
        '/api/v1/products/{product_pk}/images/search/{task_id}/',
    )
    image_data = _resolve(api_schema, image_response['properties']['data'])
    assert image_data['required'] == ['state']
    assert {
        'saved_count', 'found_count', 'rejected_count', 'eligible_count',
        'download_failed_count', 'reason_code', 'message', 'sources',
        'errors', 'cached', 'product_image_ids',
    } <= set(image_data['properties'])

    autoload = _success_schema(
        api_schema,
        '/api/v1/accounts/{id}/autoload-status/',
    )
    assert 'activate_url' in autoload['properties']
    assert 'activate_url' not in autoload['required']

    product = api_schema['components']['schemas']['Product']
    assert product['properties']['catalog_category']['nullable'] is True
    assert product['properties']['catalog_classification']['nullable'] is True

    listing_detail = api_schema['components']['schemas']['ListingDetail']
    assert listing_detail['properties']['catalog_category']['nullable'] is True
    assert '$ref' in listing_detail['properties']['images']['items']


def test_market_research_schema_is_structured_and_paginated(api_schema):
    comparison_response = _success_schema(
        api_schema,
        '/api/v1/listings/{listing_pk}/market-comparison/',
    )
    comparison = _resolve(api_schema, comparison_response['properties']['data'])
    assert set(comparison['properties']) == {
        'listing_id', 'product_id', 'base_price', 'listing_price',
        'catalog_offers', 'internet_offers', 'statistics', 'region',
        'freshness', 'active_run', 'latest_run', 'warnings',
    }

    for path_value in (
        '/api/v1/web-research/runs/',
        '/api/v1/products/{product_pk}/market-offers/',
    ):
        parameters = {
            item['name'] for item in api_schema['paths'][path_value]['get']['parameters']
        }
        assert {'page', 'page_size'} <= parameters
