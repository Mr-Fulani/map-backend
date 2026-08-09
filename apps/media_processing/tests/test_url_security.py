import socket
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from apps.core.image_security import ImagePixelLimitExceeded, validate_image_pixel_budget
from apps.core.url_security import (
    REDIRECT_NONE,
    TRUSTED_PUBLIC_HTTP_PROXY_URL,
    RequestDeadlineExceeded,
    ResponseTooLarge,
    UnsafePublicURL,
    _PinnedIPAdapter,
    _TrustedProxyAdapter,
    is_safe_public_http_url,
    read_response_limited,
    request_public_http_url,
    resolve_public_http_url,
)


PUBLIC_IP = '8.8.8.8'
SECOND_PUBLIC_IP = '1.1.1.1'


class FakeStreamResponse:
    def __init__(self, status_code=200, *, headers=None, chunks=None, url=''):
        self.status_code = status_code
        self.headers = headers or {}
        self.chunks = list(chunks or [])
        self.url = url
        self.encoding = 'utf-8'
        self.closed = False
        self.closed_event = threading.Event()
        self.iterated = False

    @property
    def content(self):
        return getattr(self, '_content', b'')

    def iter_content(self, chunk_size):
        self.iterated = True
        yield from self.chunks

    def close(self):
        self.closed = True
        self.closed_event.set()


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.trust_env = True
        self.adapters = {}
        self.mounted = []
        self.requests = []
        self.closed = False
        self.closed_event = threading.Event()

    def mount(self, prefix, adapter):
        self.adapters[prefix] = adapter
        self.mounted.append((prefix, adapter))

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if not self.response.url:
            self.response.url = url
        return self.response

    def close(self):
        self.closed = True
        self.closed_event.set()


def public_resolver(host, port, **_kwargs):
    address = SECOND_PUBLIC_IP if host == 'cdn.example.com' else PUBLIC_IP
    return [(
        socket.AF_INET,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        '',
        (address, port),
    )]


def test_public_https_url_is_allowed():
    assert is_safe_public_http_url('https://cdn.example.com/image.jpg') is True


def test_local_private_and_special_urls_are_rejected():
    assert is_safe_public_http_url('http://localhost/image.jpg') is False
    assert is_safe_public_http_url('http://127.0.0.1/image.jpg') is False
    assert is_safe_public_http_url('http://169.254.169.254/latest/meta-data/') is False
    assert is_safe_public_http_url('http://10.0.0.5/image.jpg') is False
    assert is_safe_public_http_url('http://100.64.0.1/image.jpg') is False
    assert is_safe_public_http_url('http://[64:ff9b::7f00:1]/') is False
    assert is_safe_public_http_url('http://8.8.8.8:0/') is False


def test_resolution_is_frozen_once_for_the_original_hostname():
    resolver = MagicMock(side_effect=public_resolver)
    response = FakeStreamResponse(chunks=[b'ok'])
    session = FakeSession(response)

    with patch('apps.core.url_security.requests.Session', return_value=session):
        result = request_public_http_url(
            'https://origin.example/path',
            timeout=(2, 5),
            max_response_bytes=10,
            resolver=resolver,
        )

    assert result.content == b'ok'
    resolver.assert_called_once()
    assert resolver.call_args.args == ('origin.example', 443)
    assert session.trust_env is False
    assert session.requests[0][2]['proxies'] == {}
    assert session.requests[0][2]['stream'] is True
    assert session.requests[0][2]['headers']['Accept-Encoding'] == 'identity'
    assert session.mounted[0][1].target.pinned_ip == PUBLIC_IP
    assert response.closed_event.wait(timeout=0.5)
    assert session.closed_event.wait(timeout=0.5)


def test_mixed_public_and_private_dns_answers_fail_closed():
    def mixed_resolver(_host, port, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', (PUBLIC_IP, port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port)),
        ]

    with pytest.raises(UnsafePublicURL):
        resolve_public_http_url(
            'https://rebinding.example/path',
            resolver=mixed_resolver,
        )


def test_pinned_adapter_uses_ip_but_preserves_sni_certificate_name_and_host():
    target = resolve_public_http_url(
        'https://origin.example:8443/path',
        resolver=public_resolver,
    )
    adapter = _PinnedIPAdapter(target)
    adapter.poolmanager.connection_from_host = MagicMock(return_value='pool')
    prepared = requests.Request(
        'GET',
        target.url,
        headers={'Host': 'attacker.invalid'},
    ).prepare()

    connection = adapter.get_connection_with_tls_context(
        prepared,
        verify=True,
        proxies={},
        cert=None,
    )
    adapter.add_headers(prepared)

    assert connection == 'pool'
    call = adapter.poolmanager.connection_from_host.call_args
    assert call.args[0] == PUBLIC_IP
    assert call.kwargs['port'] == 8443
    assert call.kwargs['scheme'] == 'https'
    assert call.kwargs['pool_kwargs']['server_hostname'] == 'origin.example'
    assert call.kwargs['pool_kwargs']['assert_hostname'] == 'origin.example'
    assert prepared.headers['Host'] == 'origin.example:8443'
    assert adapter.max_retries.total == 0


def test_pinned_adapter_rejects_proxy():
    target = resolve_public_http_url(
        'https://origin.example/path',
        resolver=public_resolver,
    )
    adapter = _PinnedIPAdapter(target)
    prepared = requests.Request('GET', target.url).prepare()

    with pytest.raises(UnsafePublicURL, match='Proxy'):
        adapter.get_connection_with_tls_context(
            prepared,
            verify=True,
            proxies={'https': 'http://proxy.internal:8080'},
            cert=None,
        )


def test_production_transport_pre_resolves_then_uses_only_trusted_proxy(
    settings,
    monkeypatch,
):
    settings.PUBLIC_HTTP_PROXY_URL = TRUSTED_PUBLIC_HTTP_PROXY_URL
    monkeypatch.setenv('HTTPS_PROXY', 'http://attacker.invalid:8080')
    resolver = MagicMock(side_effect=public_resolver)
    response = FakeStreamResponse(chunks=[b'ok'])
    session = FakeSession(response)

    with patch('apps.core.url_security.requests.Session', return_value=session):
        result = request_public_http_url(
            'https://origin.example/path',
            timeout=(2, 5),
            max_response_bytes=10,
            resolver=resolver,
        )

    assert result.content == b'ok'
    resolver.assert_called_once()
    assert resolver.call_args.args == ('origin.example', 443)
    assert session.trust_env is False
    assert session.requests[0][1] == 'https://origin.example/path'
    assert session.requests[0][2]['proxies'] == {
        'https': TRUSTED_PUBLIC_HTTP_PROXY_URL,
    }
    adapter = session.mounted[0][1]
    assert isinstance(adapter, _TrustedProxyAdapter)
    assert adapter.target.approved_ips == (PUBLIC_IP,)
    assert adapter.max_retries.total == 0


def test_public_transport_rejects_arbitrary_configured_proxy(settings):
    settings.PUBLIC_HTTP_PROXY_URL = 'http://attacker.invalid:8080'
    session_factory = MagicMock()

    with patch('apps.core.url_security.requests.Session', session_factory):
        with pytest.raises(UnsafePublicURL, match='доверенный'):
            request_public_http_url(
                'https://origin.example/path',
                timeout=5,
                max_response_bytes=10,
                resolver=public_resolver,
            )

    session_factory.assert_not_called()


def test_trusted_proxy_adapter_rejects_other_proxy_and_origin():
    target = resolve_public_http_url(
        'https://origin.example/path',
        resolver=public_resolver,
    )
    adapter = _TrustedProxyAdapter(target, TRUSTED_PUBLIC_HTTP_PROXY_URL)
    prepared = requests.Request(
        'GET',
        target.url,
        headers={'Host': 'attacker.invalid'},
    ).prepare()

    with patch.object(
        requests.adapters.HTTPAdapter,
        'get_connection_with_tls_context',
        return_value='proxy-pool',
    ) as base_connection:
        connection = adapter.get_connection_with_tls_context(
            prepared,
            verify=True,
            proxies={'https': TRUSTED_PUBLIC_HTTP_PROXY_URL},
            cert=None,
        )
    adapter.add_headers(prepared)

    assert connection == 'proxy-pool'
    assert prepared.headers['Host'] == 'origin.example'
    assert base_connection.call_args.kwargs['proxies'] == {
        'https': TRUSTED_PUBLIC_HTTP_PROXY_URL,
    }

    with pytest.raises(UnsafePublicURL, match='доверенный'):
        adapter.get_connection_with_tls_context(
            prepared,
            verify=True,
            proxies={'https': 'http://attacker.invalid:8080'},
            cert=None,
        )

    wrong_origin = requests.Request('GET', 'https://cdn.example.com/path').prepare()
    with pytest.raises(UnsafePublicURL, match='origin'):
        adapter.get_connection_with_tls_context(
            wrong_origin,
            verify=True,
            proxies={'https': TRUSTED_PUBLIC_HTTP_PROXY_URL},
            cert=None,
        )


@pytest.mark.parametrize('scheme', ('http', 'https'))
def test_real_requests_session_passes_only_explicit_proxy_to_adapter(
    monkeypatch,
    scheme,
):
    target = resolve_public_http_url(
        f'{scheme}://origin.example/path',
        resolver=public_resolver,
    )
    adapter = _TrustedProxyAdapter(target, TRUSTED_PUBLIC_HTTP_PROXY_URL)
    original_get_connection = adapter.get_connection_with_tls_context
    captured = {}

    class StopBeforeNetwork(Exception):
        pass

    def capture_after_validation(request, verify, proxies=None, cert=None):
        captured['proxies'] = dict(proxies or {})
        original_get_connection(
            request,
            verify,
            proxies=proxies,
            cert=cert,
        )
        raise StopBeforeNetwork

    adapter.get_connection_with_tls_context = capture_after_validation
    session = requests.Session()
    session.trust_env = False
    session.adapters.clear()
    session.mount(f'{scheme}://', adapter)
    monkeypatch.setenv(f'{scheme.upper()}_PROXY', 'http://attacker.invalid:8080')
    try:
        with pytest.raises(StopBeforeNetwork):
            session.request(
                'GET',
                target.url,
                timeout=1,
                allow_redirects=False,
                proxies={scheme: TRUSTED_PUBLIC_HTTP_PROXY_URL},
            )
    finally:
        session.close()

    assert captured['proxies'] == {scheme: TRUSTED_PUBLIC_HTTP_PROXY_URL}


def test_cross_origin_redirect_strips_auth_and_sensitive_headers():
    first_response = FakeStreamResponse(
        302,
        headers={'Location': 'https://cdn.example.com/final'},
        url='https://origin.example/start',
    )
    second_response = FakeStreamResponse(
        200,
        chunks=[b'image'],
        url='https://cdn.example.com/final',
    )
    first_session = FakeSession(first_response)
    second_session = FakeSession(second_response)

    with patch(
        'apps.core.url_security.requests.Session',
        side_effect=[first_session, second_session],
    ):
        result = request_public_http_url(
            'https://origin.example/start',
            timeout=5,
            headers={
                'Authorization': 'Bearer secret',
                'Cookie': 'session=secret',
                'X-Custom-Secret': 'also-secret',
                'User-Agent': 'MAP-Test/1.0',
            },
            auth=('user', 'password'),
            max_response_bytes=10,
            resolver=public_resolver,
        )

    second_kwargs = second_session.requests[0][2]
    assert result.content == b'image'
    assert second_kwargs['auth'] is None
    assert second_kwargs['headers'] == {
        'User-Agent': 'MAP-Test/1.0',
        'Accept-Encoding': 'identity',
    }
    assert first_response.closed is True
    assert second_response.closed is True


def test_same_origin_redirect_keeps_basic_auth():
    redirect = FakeStreamResponse(
        302,
        headers={'Location': '/final'},
        url='https://origin.example/start',
    )
    final = FakeStreamResponse(200, chunks=[b'ok'], url='https://origin.example/final')
    first_session = FakeSession(redirect)
    second_session = FakeSession(final)

    with patch(
        'apps.core.url_security.requests.Session',
        side_effect=[first_session, second_session],
    ):
        request_public_http_url(
            'https://origin.example/start',
            timeout=5,
            auth=('user', 'password'),
            max_response_bytes=10,
            resolver=public_resolver,
        )

    assert second_session.requests[0][2]['auth'] == ('user', 'password')


def test_basic_auth_is_rejected_before_plain_http_request():
    session = FakeSession(FakeStreamResponse(chunks=[b'ok']))

    with patch('apps.core.url_security.requests.Session', return_value=session):
        with pytest.raises(UnsafePublicURL, match='только по HTTPS'):
            request_public_http_url(
                'http://origin.example/private',
                timeout=5,
                auth=('user', 'password'),
                max_response_bytes=10,
                resolver=public_resolver,
            )

    assert session.requests == []


def test_redirect_policy_none_stops_before_second_request():
    redirect = FakeStreamResponse(
        302,
        headers={'Location': 'https://cdn.example.com/final'},
    )
    session = FakeSession(redirect)

    with patch('apps.core.url_security.requests.Session', return_value=session):
        with pytest.raises(UnsafePublicURL, match='redirect запрещён'):
            request_public_http_url(
                'https://origin.example/start',
                timeout=5,
                status_only=True,
                redirect_policy=REDIRECT_NONE,
                resolver=public_resolver,
            )

    assert response_was_not_buffered(redirect)
    assert redirect.closed is True


def test_post_redirect_is_rejected_before_replaying_the_body():
    redirect = FakeStreamResponse(307, headers={'Location': '/again'})
    session = FakeSession(redirect)

    with patch('apps.core.url_security.requests.Session', return_value=session):
        with pytest.raises(UnsafePublicURL, match='non-idempotent'):
            request_public_http_url(
                'https://origin.example/webhook',
                method='POST',
                data=b'secret',
                timeout=5,
                status_only=True,
                resolver=public_resolver,
            )

    assert len(session.requests) == 1


def response_was_not_buffered(response):
    return response.iterated is False


def test_status_only_does_not_read_response_body():
    response = FakeStreamResponse(chunks=[b'x' * 100])
    session = FakeSession(response)

    with patch('apps.core.url_security.requests.Session', return_value=session):
        result = request_public_http_url(
            'https://origin.example/status',
            method='POST',
            timeout=5,
            status_only=True,
            redirect_policy=REDIRECT_NONE,
            resolver=public_resolver,
        )

    assert result.content == b''
    assert response_was_not_buffered(response)
    assert response.closed is True


def test_bounded_request_rejects_oversized_chunked_body_and_closes():
    response = FakeStreamResponse(chunks=[b'a' * 6, b'b' * 5])
    session = FakeSession(response)

    with patch('apps.core.url_security.requests.Session', return_value=session):
        with pytest.raises(ResponseTooLarge):
            request_public_http_url(
                'https://origin.example/large',
                timeout=5,
                max_response_bytes=10,
                resolver=public_resolver,
            )

    assert response.closed is True
    assert session.closed is True


def test_bounded_request_aborts_slow_drip_body_at_total_deadline():
    release = threading.Event()

    class BlockingResponse(FakeStreamResponse):
        def iter_content(self, chunk_size):
            del chunk_size
            assert release.wait(timeout=1)
            yield b'late'

        def close(self):
            super().close()
            release.set()

    response = BlockingResponse()
    session = FakeSession(response)

    with patch('apps.core.url_security.requests.Session', return_value=session):
        with pytest.raises(ResponseTooLarge, match='лимит времени'):
            request_public_http_url(
                'https://origin.example/slow',
                timeout=5,
                max_response_bytes=10,
                max_elapsed_seconds=0.02,
                resolver=public_resolver,
            )

    assert response.closed_event.wait(timeout=0.5)
    assert session.closed_event.wait(timeout=0.5)


def test_public_request_deadline_includes_dns_resolution():
    resolver_entered = threading.Event()
    release_resolver = threading.Event()
    session_factory = MagicMock()

    def slow_resolver(host, port, **kwargs):
        resolver_entered.set()
        release_resolver.wait(timeout=1)
        return public_resolver(host, port, **kwargs)

    started_at = time.monotonic()
    try:
        with patch('apps.core.url_security.requests.Session', session_factory):
            with pytest.raises(RequestDeadlineExceeded):
                request_public_http_url(
                    'https://origin.example/slow-dns',
                    timeout=5,
                    max_response_bytes=10,
                    max_elapsed_seconds=0.02,
                    resolver=slow_resolver,
                )
    finally:
        release_resolver.set()

    assert resolver_entered.is_set()
    assert time.monotonic() - started_at < 0.5
    session_factory.assert_not_called()


def test_public_request_deadline_includes_waiting_for_response_headers():
    request_entered = threading.Event()
    release_request = threading.Event()
    response = FakeStreamResponse(chunks=[b'late'])

    class SlowHeaderSession(FakeSession):
        def request(self, method, url, **kwargs):
            request_entered.set()
            release_request.wait(timeout=1)
            return super().request(method, url, **kwargs)

    session = SlowHeaderSession(response)
    started_at = time.monotonic()
    try:
        with patch('apps.core.url_security.requests.Session', return_value=session):
            with pytest.raises(RequestDeadlineExceeded):
                request_public_http_url(
                    'https://origin.example/slow-headers',
                    timeout=5,
                    max_response_bytes=10,
                    max_elapsed_seconds=0.02,
                    resolver=public_resolver,
                )
    finally:
        release_request.set()

    assert request_entered.is_set()
    assert time.monotonic() - started_at < 0.5
    assert response.closed_event.wait(timeout=0.5)
    assert session.closed_event.wait(timeout=0.5)


def test_streamed_response_rejects_oversized_content_length():
    response = MagicMock(headers={'Content-Length': '101'})

    with pytest.raises(ResponseTooLarge):
        read_response_limited(response, max_bytes=100)

    response.iter_content.assert_not_called()


def test_streamed_response_rejects_compression_before_decoding():
    response = MagicMock(headers={'Content-Encoding': 'gzip'})

    with pytest.raises(ResponseTooLarge, match='Сжатые'):
        read_response_limited(response, max_bytes=100)

    response.iter_content.assert_not_called()


def test_decoded_pixel_budget_is_checked_before_image_load(settings):
    settings.MAX_DECODED_IMAGE_PIXELS = 99
    image = MagicMock(size=(10, 10))

    with pytest.raises(ImagePixelLimitExceeded):
        validate_image_pixel_budget(image)

    image.load.assert_not_called()
