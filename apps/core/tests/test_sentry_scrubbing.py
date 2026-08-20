from config.sentry_scrubbing import (
    scrub_sentry_breadcrumb,
    scrub_sentry_event,
    scrub_sentry_metric,
)


def test_sentry_event_removes_auth_body_headers_locals_and_url_secrets():
    event = {
        'request': {
            'url': 'https://app.example/api/v1/auth/confirm-email/?token=secret',
            'query_string': 'token=secret',
            'data': {'current_password': 'hunter2'},
            'cookies': {'map_refresh': 'jwt'},
            'headers': {
                'Authorization': 'Bearer access',
                'X-Request-ID': 'safe-id',
            },
        },
        'exception': {
            'values': [{
                'stacktrace': {
                    'frames': [{
                        'vars': {'reset_url': 'https://app/reset#token=secret'},
                    }],
                },
            }],
        },
        'breadcrumbs': {'values': [{'message': 'removed for auth'}]},
    }

    scrubbed = scrub_sentry_event(event)

    assert scrubbed['request']['url'] == 'https://app.example/api/v1/auth/confirm-email/'
    assert scrubbed['request']['query_string'] == '[Filtered]'
    assert scrubbed['request']['data'] == '[Filtered]'
    assert scrubbed['request']['cookies'] == '[Filtered]'
    assert scrubbed['request']['headers']['Authorization'] == '[Filtered]'
    assert scrubbed['request']['headers']['X-Request-ID'] == 'safe-id'
    assert scrubbed['exception']['values'][0]['stacktrace']['frames'][0]['vars'][
        'reset_url'
    ] == '[Filtered]'
    assert scrubbed['breadcrumbs'] == {'values': []}


def test_sentry_breadcrumb_strips_url_query_and_nested_credentials():
    breadcrumb = {
        'data': {
            'url': 'https://provider.example/send?api_key=secret',
            'credentials': {'password': 'secret'},
        },
    }

    scrubbed = scrub_sentry_breadcrumb(breadcrumb)

    assert scrubbed['data']['url'] == 'https://provider.example/send'
    assert scrubbed['data']['credentials'] == '[Filtered]'


def test_sentry_scrubs_feed_location_referer_and_every_named_url():
    capability = 'stable-capability-must-not-leak'
    event = {
        'request': {
            'url': f'https://app.example/marketplace-feeds/v1/feed.xml?key={capability}',
            'query_string': f'key={capability}',
            'headers': {
                'Referer': f'https://app.example/feed?key={capability}',
            },
        },
        'extra': {
            'feed_url': f'https://app.example/feed?key={capability}',
            'presigned_url': f'https://storage.example/object?signature={capability}',
            'response': {
                'Location': f'https://storage.example/object?signature={capability}',
            },
        },
    }

    scrubbed = scrub_sentry_event(event)

    assert capability not in repr(scrubbed)
    assert scrubbed['request']['headers']['Referer'] == 'https://app.example/feed'
    assert scrubbed['extra']['feed_url'] == 'https://app.example/feed'
    assert scrubbed['extra']['presigned_url'] == 'https://storage.example/object'
    assert scrubbed['extra']['response']['Location'] == 'https://storage.example/object'


def test_sentry_transaction_event_scrubs_feed_capability_query():
    capability = 'sampled-transaction-capability-must-not-leak'
    transaction = {
        'type': 'transaction',
        'transaction': 'GET /marketplace-feeds/v1/feed.xml',
        'request': {
            'url': (
                'https://app.example/marketplace-feeds/v1/feed.xml'
                f'?id=00000000-0000-0000-0000-000000000001&key={capability}'
            ),
            'query_string': (
                'id=00000000-0000-0000-0000-000000000001'
                f'&key={capability}'
            ),
        },
    }

    scrubbed = scrub_sentry_event(transaction)

    assert capability not in repr(scrubbed)
    assert scrubbed['request']['url'] == (
        'https://app.example/marketplace-feeds/v1/feed.xml'
    )
    assert scrubbed['request']['query_string'] == '[Filtered]'


def test_sentry_recursively_scrubs_nested_query_string():
    capability = 'nested-capability-must-not-leak'
    event = {
        'contexts': {
            'trace': {
                'query_string': f'id=endpoint-id&key={capability}',
            },
        },
    }

    scrubbed = scrub_sentry_event(event)

    assert capability not in repr(scrubbed)
    assert scrubbed['contexts']['trace']['query_string'] == '[Filtered]'


def test_sentry_url_scrubber_removes_embedded_basic_auth_credentials():
    event = {
        'request': {
            'url': 'https://api-user:api-password@example.com:8443/path?token=secret',
        },
    }

    scrubbed = scrub_sentry_event(event)

    assert scrubbed['request']['url'] == 'https://example.com:8443/path'


def test_sentry_metric_keeps_only_map_allowlisted_dimensions():
    metric = {
        'name': 'map.provider.request',
        'attributes': {
            'provider': 'avito',
            'operation': 'status',
            'outcome': 'failure',
            'response_class': '5xx',
            'tenant_id': '123',
            'request_url': 'https://secret.example/path',
        },
    }

    scrubbed = scrub_sentry_metric(metric)

    assert scrubbed['attributes'] == {
        'provider': 'avito',
        'operation': 'status',
        'outcome': 'failure',
        'response_class': '5xx',
    }
    external = {'name': 'sentry.sdk.metric', 'attributes': {'tenant_id': '123'}}
    assert scrub_sentry_metric(external) == external
