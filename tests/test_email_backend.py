from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from apps.core.email_backend import (
    HTTPConnectSMTP,
    HTTPProxySMTPEmailBackend,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_PROXY_URL,
)


class FakeSocket:
    def __init__(self, response: bytes):
        self.response = bytearray(response)
        self.sent = b''
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, size: int) -> bytes:
        if not self.response:
            return b''
        chunk = bytes(self.response[:size])
        del self.response[:size]
        return chunk

    def close(self) -> None:
        self.closed = True


@override_settings(EMAIL_HTTP_PROXY_URL=SMTP_PROXY_URL)
@patch('apps.core.email_backend.socket.create_connection')
def test_connect_tunnel_preserves_the_upstream_smtp_greeting(create_connection):
    greeting = b'220 smtp.sendpulse.com ESMTP ready\r\n'
    proxy_socket = FakeSocket(
        b'HTTP/1.1 200 Connection established\r\nProxy-Agent: squid\r\n\r\n'
        + greeting
    )
    create_connection.return_value = proxy_socket
    smtp = HTTPConnectSMTP()

    result = smtp._get_socket(SMTP_HOST, SMTP_PORT, 7)

    assert result is proxy_socket
    assert bytes(proxy_socket.response) == greeting
    assert proxy_socket.sent == (
        b'CONNECT smtp.sendpulse.com:587 HTTP/1.1\r\n'
        b'Host: smtp.sendpulse.com:587\r\n'
        b'Proxy-Connection: Keep-Alive\r\n\r\n'
    )
    create_connection.assert_called_once_with(('egress_proxy', 3128), 7, None)
    assert proxy_socket.closed is False


@override_settings(EMAIL_HTTP_PROXY_URL=SMTP_PROXY_URL)
@patch('apps.core.email_backend.socket.create_connection')
def test_connect_tunnel_closes_on_rejection_without_exposing_proxy_response(
    create_connection,
):
    proxy_socket = FakeSocket(
        b'HTTP/1.1 403 password=provider-secret\r\n\r\n'
    )
    create_connection.return_value = proxy_socket
    smtp = HTTPConnectSMTP()

    with pytest.raises(OSError) as error:
        smtp._get_socket(SMTP_HOST, SMTP_PORT, 7)

    assert 'provider-secret' not in str(error.value)
    assert proxy_socket.closed is True


@override_settings(EMAIL_HTTP_PROXY_URL=SMTP_PROXY_URL)
@patch('apps.core.email_backend.socket.create_connection')
def test_connect_tunnel_rejects_every_non_provider_destination(create_connection):
    smtp = HTTPConnectSMTP()

    with pytest.raises(OSError):
        smtp._get_socket('attacker.example', SMTP_PORT, 7)
    with pytest.raises(OSError):
        smtp._get_socket(SMTP_HOST, 25, 7)

    create_connection.assert_not_called()


@override_settings(EMAIL_HTTP_PROXY_URL='http://user:secret@egress_proxy:3128')
def test_connect_tunnel_rejects_configurable_or_credentialed_proxy_urls():
    smtp = HTTPConnectSMTP()

    with pytest.raises(ImproperlyConfigured) as error:
        smtp._get_socket(SMTP_HOST, SMTP_PORT, 7)

    assert 'secret' not in str(error.value)


def test_django_backend_uses_connect_transport_with_starttls_not_implicit_tls():
    backend = HTTPProxySMTPEmailBackend(use_tls=True, use_ssl=False)

    assert backend.connection_class is HTTPConnectSMTP

    implicit_tls_backend = HTTPProxySMTPEmailBackend(use_tls=False, use_ssl=True)
    with pytest.raises(ImproperlyConfigured):
        _ = implicit_tls_backend.connection_class
