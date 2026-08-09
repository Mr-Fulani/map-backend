from config.sentry_scrubbing import scrub_sentry_breadcrumb, scrub_sentry_event


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


def test_sentry_url_scrubber_removes_embedded_basic_auth_credentials():
    event = {
        'request': {
            'url': 'https://api-user:api-password@example.com:8443/path?token=secret',
        },
    }

    scrubbed = scrub_sentry_event(event)

    assert scrubbed['request']['url'] == 'https://example.com:8443/path'
