from io import StringIO
from unittest.mock import Mock, patch

import pytest
import requests
from django.test import override_settings

from apps.notifications.management.commands import setup_telegram_webhook
from apps.notifications.management.commands import telegram_poll
from apps.notifications.telegram_api import (
    TelegramAPIError,
    expect_boolean_result,
    request_telegram_json,
)


def response_with(payload, *, status_code=200):
    response = Mock(status_code=status_code)
    response.json.return_value = payload
    return response


def test_telegram_json_request_is_bounded_and_does_not_follow_redirects():
    requester = Mock()
    response = response_with({'ok': True, 'result': True})

    with patch(
        'apps.notifications.telegram_api.bounded_http_request',
        return_value=response,
    ) as bounded_request:
        result = request_telegram_json(
            requester,
            'https://api.telegram.org/bottest/deleteWebhook',
            timeout=(5.0, 10.0),
            max_elapsed_seconds=15.0,
            max_bytes=65536,
        )

    assert result == {'ok': True, 'result': True}
    bounded_request.assert_called_once_with(
        requester,
        'https://api.telegram.org/bottest/deleteWebhook',
        timeout=(5.0, 10.0),
        allow_redirects=False,
        max_elapsed_seconds=15.0,
        max_bytes=65536,
    )


def test_telegram_json_request_does_not_leak_token_from_transport_error():
    secret = '123456:SUPER_SECRET_TOKEN'
    transport_error = requests.Timeout(
        f'https://api.telegram.org/bot{secret}/getUpdates timed out'
    )

    with patch(
        'apps.notifications.telegram_api.bounded_http_request',
        side_effect=transport_error,
    ), pytest.raises(TelegramAPIError) as exc_info:
        request_telegram_json(
            requests.get,
            f'https://api.telegram.org/bot{secret}/getUpdates',
            timeout=(5.0, 35.0),
            max_elapsed_seconds=40.0,
            max_bytes=1024,
        )

    assert secret not in str(exc_info.value)
    assert 'Timeout' in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@pytest.mark.parametrize(
    'payload',
    [
        [],
        {'ok': 'true', 'result': []},
        {'ok': True},
        {'ok': False, 'description': {'unexpected': 'object'}},
        {'ok': False, 'description': 'rejected'},
        {'ok': False, 'error_code': True, 'description': 'rejected'},
    ],
)
def test_telegram_json_request_rejects_malformed_envelopes(payload):
    with patch(
        'apps.notifications.telegram_api.bounded_http_request',
        return_value=response_with(payload),
    ), pytest.raises(TelegramAPIError):
        request_telegram_json(
            requests.get,
            'https://api.telegram.org/bottest/getUpdates',
            timeout=(5.0, 35.0),
            max_elapsed_seconds=40.0,
            max_bytes=1024,
        )


@pytest.mark.parametrize('result', [False, 0, 1, None, {}, []])
def test_boolean_webhook_result_is_strict(result):
    with pytest.raises(TelegramAPIError):
        expect_boolean_result({'ok': True, 'result': result})


@override_settings(
    TELEGRAM_BOT_TOKEN='123:test-token',
    TRUSTED_API_RESPONSE_MAX_BYTES=5 * 1024 * 1024,
)
def test_setup_command_uses_control_timeouts_deadline_and_small_cap():
    with patch(
        'apps.notifications.management.commands.setup_telegram_webhook.request_telegram_json',
        return_value={'ok': True, 'result': True},
    ) as request_json:
        result = setup_telegram_webhook.Command._request(
            requests.post,
            'https://api.telegram.org/bot123:test-token/deleteWebhook',
        )

    assert result == {'ok': True, 'result': True}
    assert request_json.call_args.kwargs == {
        'timeout': (5.0, 10.0),
        'max_elapsed_seconds': 15.0,
        'max_bytes': 64 * 1024,
    }


@override_settings(
    TELEGRAM_BOT_TOKEN='123:test-token',
    TRUSTED_API_RESPONSE_MAX_BYTES=5 * 1024 * 1024,
)
def test_poll_command_uses_long_poll_timeouts_deadline_and_cap():
    with patch(
        'apps.notifications.management.commands.telegram_poll.request_telegram_json',
        return_value={'ok': True, 'result': []},
    ) as request_json:
        result = telegram_poll.Command._request_updates(
            requests.get,
            'https://api.telegram.org/bot123:test-token/getUpdates',
            params={'offset': 10, 'timeout': 30},
        )

    assert result == {'ok': True, 'result': []}
    assert request_json.call_args.kwargs == {
        'timeout': (5.0, 35.0),
        'max_elapsed_seconds': 40.0,
        'max_bytes': 1024 * 1024,
        'params': {'offset': 10, 'timeout': 30},
    }


@override_settings(
    TELEGRAM_BOT_TOKEN='123:test-token',
    TRUSTED_API_RESPONSE_MAX_BYTES=5 * 1024 * 1024,
)
def test_poll_delete_webhook_uses_explicit_control_timeouts():
    with patch(
        'apps.notifications.management.commands.telegram_poll.request_telegram_json',
        return_value={'ok': True, 'result': True},
    ) as request_json:
        result = telegram_poll.Command._request_control(
            requests.post,
            'https://api.telegram.org/bot123:test-token/deleteWebhook',
        )

    assert result == {'ok': True, 'result': True}
    assert request_json.call_args.kwargs == {
        'timeout': (5.0, 10.0),
        'max_elapsed_seconds': 15.0,
        'max_bytes': 64 * 1024,
    }


@pytest.mark.parametrize(
    'result',
    [
        {},
        [None],
        [{}],
        [{'update_id': True}],
        [{'update_id': -1}],
        [{'update_id': 1, 'message': 'not-an-object'}],
    ],
)
def test_poll_command_rejects_invalid_update_shapes(result):
    with pytest.raises(TelegramAPIError):
        telegram_poll.Command._validate_updates(result)


@override_settings(TELEGRAM_BOT_TOKEN='123:test-token')
def test_poll_command_stops_when_delete_webhook_fails():
    stdout = StringIO()
    stderr = StringIO()
    command = telegram_poll.Command(stdout=stdout, stderr=stderr)

    with patch.object(
        command,
        '_request_control',
        side_effect=TelegramAPIError('safe failure'),
    ), patch.object(command, '_request_updates') as get_updates:
        command.handle()

    assert 'Ошибка deleteWebhook: safe failure' in stderr.getvalue()
    assert 'Polling запущен' not in stdout.getvalue()
    get_updates.assert_not_called()
