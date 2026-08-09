from unittest.mock import Mock, patch

from django.test import override_settings

from apps.notifications.telegram import TelegramNotifier


@override_settings(TELEGRAM_BOT_TOKEN='test-token', TRUSTED_API_RESPONSE_MAX_BYTES=1024)
def test_telegram_notifier_uses_closed_streaming_response():
    response = Mock(status_code=200, headers={})
    response.iter_content.return_value = iter([
        b'{"ok":true,"result":{"message_id":1}}',
    ])
    response.json.return_value = {
        'ok': True,
        'result': {'message_id': 1},
    }

    with patch('apps.notifications.telegram.requests.post', return_value=response) as post:
        assert TelegramNotifier().send('123', 'message') is True

    assert post.call_args.kwargs['stream'] is True
    assert post.call_args.kwargs['headers']['Accept-Encoding'] == 'identity'
    assert post.call_args.kwargs['allow_redirects'] is False
    assert post.call_args.kwargs['timeout'] == (5.0, 10.0)
    response.close.assert_called_once_with()
