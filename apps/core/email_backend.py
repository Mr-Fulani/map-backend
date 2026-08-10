import re
import smtplib
import socket
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail.backends.smtp import EmailBackend


SMTP_HOST = 'smtp.resend.com'
SMTP_PORT = 587
SMTP_PROXY_URL = 'http://egress_proxy:3128'
MAX_PROXY_RESPONSE_HEADER_BYTES = 8192
_CONNECT_SUCCESS = re.compile(rb'^HTTP/1\.[01] 200(?:[ \t]|$)')


def _validated_proxy_endpoint() -> tuple[str, int]:
    """Return the fixed internal proxy endpoint or fail closed."""
    proxy_url = getattr(settings, 'EMAIL_HTTP_PROXY_URL', '')
    if proxy_url != SMTP_PROXY_URL:
        raise ImproperlyConfigured(
            'EMAIL_HTTP_PROXY_URL must use the fixed production egress proxy.',
        )

    parsed = urlsplit(proxy_url)
    if (
        parsed.scheme != 'http'
        or parsed.hostname != 'egress_proxy'
        or parsed.port != 3128
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ('', '/')
        or parsed.query
        or parsed.fragment
    ):
        raise ImproperlyConfigured(
            'EMAIL_HTTP_PROXY_URL must be a credential-free HTTP proxy URL.',
        )
    return parsed.hostname, parsed.port


class HTTPConnectSMTP(smtplib.SMTP):
    """SMTP transport that reaches the fixed provider through HTTP CONNECT."""

    def _get_socket(self, host, port, timeout):
        if host != SMTP_HOST or port != SMTP_PORT:
            raise OSError('SMTP destination is not allowed.')

        proxy_host, proxy_port = _validated_proxy_endpoint()
        proxy_socket = socket.create_connection(
            (proxy_host, proxy_port),
            timeout,
            self.source_address,
        )
        try:
            destination = f'{SMTP_HOST}:{SMTP_PORT}'
            request = (
                f'CONNECT {destination} HTTP/1.1\r\n'
                f'Host: {destination}\r\n'
                'Proxy-Connection: Keep-Alive\r\n'
                '\r\n'
            ).encode('ascii')
            proxy_socket.sendall(request)

            # Read exactly through the HTTP header terminator. A proxy may send
            # the upstream SMTP greeting in the same packet; consuming any of it
            # here would make smtplib wait forever for a greeting it cannot see.
            response_header = bytearray()
            while not response_header.endswith(b'\r\n\r\n'):
                chunk = proxy_socket.recv(1)
                if not chunk:
                    raise OSError('SMTP proxy closed the CONNECT handshake.')
                response_header.extend(chunk)
                if len(response_header) > MAX_PROXY_RESPONSE_HEADER_BYTES:
                    raise OSError('SMTP proxy response header is too large.')

            status_line = bytes(response_header).split(b'\r\n', 1)[0]
            if _CONNECT_SUCCESS.match(status_line) is None:
                raise OSError('SMTP proxy rejected the CONNECT tunnel.')
        except BaseException:
            try:
                proxy_socket.close()
            except OSError:
                pass
            raise

        return proxy_socket


class HTTPProxySMTPEmailBackend(EmailBackend):
    """Django SMTP backend with a fixed, policy-controlled CONNECT transport."""

    @property
    def connection_class(self):
        if self.use_ssl:
            raise ImproperlyConfigured(
                'Implicit SMTP TLS is unsupported; use STARTTLS on port 587.',
            )
        return HTTPConnectSMTP
