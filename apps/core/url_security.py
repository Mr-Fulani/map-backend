import ipaddress
import math
import socket
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from apps.core.http_deadlines import (
    HTTPDeadlineExceeded,
    enforce_response_deadline,
    run_with_deadline,
)


REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
DEFAULT_MAX_REDIRECTS = 3
DEFAULT_MAX_ELAPSED_SECONDS = 60.0
REDIRECT_NONE = 'none'
REDIRECT_SAME_ORIGIN = 'same-origin'
REDIRECT_PUBLIC = 'public'
REDIRECT_POLICIES = frozenset({
    REDIRECT_NONE,
    REDIRECT_SAME_ORIGIN,
    REDIRECT_PUBLIC,
})
MAX_PUBLIC_URL_LENGTH = 4096
TRUSTED_PUBLIC_HTTP_PROXY_URL = 'http://egress_proxy:3128'
SAFE_CROSS_ORIGIN_HEADERS = frozenset({
    'accept',
    'user-agent',
})
SENSITIVE_TRANSPORT_HEADERS = frozenset({
    'authorization',
    'cookie',
    'proxy-authorization',
})
_NAT64_NETWORKS = (
    ipaddress.ip_network('64:ff9b::/96'),
    ipaddress.ip_network('64:ff9b:1::/48'),
)


class UnsafePublicURL(ValueError):
    """The URL is invalid or can resolve outside the public Internet."""


class ResponseTooLarge(ValueError):
    """The remote response exceeded the configured byte budget."""


class RequestDeadlineExceeded(ResponseTooLarge):
    """The public response exceeded its total wall-clock budget."""


@dataclass(frozen=True)
class _ParsedPublicURL:
    url: str
    scheme: str
    hostname: str
    port: int
    authority: str

    @property
    def origin(self) -> tuple[str, str, int]:
        return self.scheme, self.hostname, self.port


@dataclass(frozen=True)
class ResolvedPublicTarget(_ParsedPublicURL):
    approved_ips: tuple[str, ...]

    @property
    def pinned_ip(self) -> str:
        return self.approved_ips[0]


def _is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if any(address in network for network in _NAT64_NETWORKS):
            return False
        embedded = address.ipv4_mapped or address.sixtofour
        if embedded is not None and not _is_public_address(str(embedded)):
            return False
        if address.teredo is not None:
            server, client = address.teredo
            if not _is_public_address(str(server)) or not _is_public_address(str(client)):
                return False
    return True


def _parse_public_http_url(value: str) -> _ParsedPublicURL:
    raw_url = str(value or '').strip()
    if not raw_url or len(raw_url) > MAX_PUBLIC_URL_LENGTH:
        raise UnsafePublicURL('Некорректная длина HTTP URL.')
    if '\\' in raw_url or any(ord(char) < 32 or ord(char) == 127 for char in raw_url):
        raise UnsafePublicURL('HTTP URL содержит запрещённые символы.')

    try:
        parsed = urlsplit(raw_url)
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or '').rstrip('.').lower()
        parsed_port = parsed.port
    except (TypeError, ValueError) as exc:
        raise UnsafePublicURL('Некорректный HTTP URL.') from exc

    if scheme not in {'http', 'https'} or not hostname:
        raise UnsafePublicURL('Разрешены только HTTP(S) URL с hostname.')
    port = parsed_port if parsed_port is not None else (443 if scheme == 'https' else 80)
    if port <= 0:
        raise UnsafePublicURL('Некорректный TCP port в HTTP URL.')
    if parsed.username is not None or parsed.password is not None:
        raise UnsafePublicURL('Credentials в HTTP URL запрещены.')
    if hostname == 'localhost' or hostname.endswith('.localhost') or '%' in hostname:
        raise UnsafePublicURL('Локальный hostname запрещён.')

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            canonical_hostname = hostname.encode('idna').decode('ascii')
        except UnicodeError as exc:
            raise UnsafePublicURL('Некорректный hostname.') from exc
    else:
        canonical_hostname = address.compressed

    default_port = 443 if scheme == 'https' else 80
    host_for_authority = (
        f'[{canonical_hostname}]' if ':' in canonical_hostname else canonical_hostname
    )
    authority = (
        host_for_authority
        if port == default_port
        else f'{host_for_authority}:{port}'
    )
    canonical_url = urlunsplit((
        scheme,
        authority,
        parsed.path or '/',
        parsed.query,
        '',
    ))
    return _ParsedPublicURL(
        url=canonical_url,
        scheme=scheme,
        hostname=canonical_hostname,
        port=port,
        authority=authority,
    )


def resolve_public_http_url(
    value: str,
    *,
    resolver: Callable | None = None,
) -> ResolvedPublicTarget:
    """Resolve once and freeze the complete set of approved public addresses."""
    parsed = _parse_public_http_url(value)
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        resolve = resolver or socket.getaddrinfo
        try:
            records = resolve(
                parsed.hostname,
                parsed.port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except OSError as exc:
            raise UnsafePublicURL('Не удалось безопасно разрешить hostname.') from exc
        addresses = []
        for family, _socktype, _proto, _canonical_name, sockaddr in records:
            if family not in {socket.AF_INET, socket.AF_INET6} or not sockaddr:
                continue
            address = ipaddress.ip_address(sockaddr[0]).compressed
            if address not in addresses:
                addresses.append(address)
    else:
        addresses = [literal.compressed]

    if not addresses or not all(_is_public_address(address) for address in addresses):
        raise UnsafePublicURL('URL не указывает только на публичные IP-адреса.')
    return ResolvedPublicTarget(
        **parsed.__dict__,
        approved_ips=tuple(addresses),
    )


def is_safe_public_http_url(value: str, *, resolve_hostname: bool = False) -> bool:
    """Validate syntax, and optionally DNS, for serializer/UI preflight only."""
    try:
        if resolve_hostname:
            resolve_public_http_url(value)
        else:
            parsed = _parse_public_http_url(value)
            try:
                address = ipaddress.ip_address(parsed.hostname)
            except ValueError:
                return True
            if not _is_public_address(str(address)):
                return False
        return True
    except UnsafePublicURL:
        return False


class _PinnedIPAdapter(HTTPAdapter):
    """Connect to a frozen IP while preserving the logical HTTP/TLS origin."""

    def __init__(self, target: ResolvedPublicTarget):
        self.target = target
        no_retry = Retry(
            total=0,
            connect=0,
            read=0,
            redirect=0,
            status=0,
            other=0,
            raise_on_redirect=False,
            raise_on_status=False,
        )
        super().__init__(max_retries=no_retry)

    def get_connection_with_tls_context(
        self,
        request,
        verify,
        proxies=None,
        cert=None,
    ):
        if proxies and any(proxies.values()):
            raise UnsafePublicURL('Proxy запрещён для DNS-pinned HTTP-запросов.')
        request_origin = _parse_public_http_url(request.url).origin
        if request_origin != self.target.origin:
            raise UnsafePublicURL('HTTP request не совпадает с pinned origin.')

        _host_params, pool_kwargs = self.build_connection_pool_key_attributes(
            request,
            verify,
            cert,
        )
        pool_kwargs = dict(pool_kwargs)
        if self.target.scheme == 'https':
            pool_kwargs.update({
                'server_hostname': self.target.hostname,
                'assert_hostname': self.target.hostname,
            })
        return self.poolmanager.connection_from_host(
            self.target.pinned_ip,
            port=self.target.port,
            scheme=self.target.scheme,
            pool_kwargs=pool_kwargs,
        )

    def add_headers(self, request, **kwargs):
        request.headers['Host'] = self.target.authority


class _TrustedProxyAdapter(HTTPAdapter):
    """Bind one admitted origin to the only trusted production proxy."""

    def __init__(self, target: ResolvedPublicTarget, proxy_url: str):
        if proxy_url != TRUSTED_PUBLIC_HTTP_PROXY_URL:
            raise UnsafePublicURL('Разрешён только доверенный public HTTP proxy.')
        self.target = target
        self.proxy_url = proxy_url
        no_retry = Retry(
            total=0,
            connect=0,
            read=0,
            redirect=0,
            status=0,
            other=0,
            raise_on_redirect=False,
            raise_on_status=False,
        )
        super().__init__(max_retries=no_retry)

    def get_connection_with_tls_context(
        self,
        request,
        verify,
        proxies=None,
        cert=None,
    ):
        expected_proxies = {self.target.scheme: self.proxy_url}
        if dict(proxies or {}) != expected_proxies:
            raise UnsafePublicURL('Разрешён только доверенный public HTTP proxy.')
        request_origin = _parse_public_http_url(request.url).origin
        if request_origin != self.target.origin:
            raise UnsafePublicURL('HTTP request не совпадает с проверенным origin.')
        return super().get_connection_with_tls_context(
            request,
            verify,
            proxies=proxies,
            cert=cert,
        )

    def add_headers(self, request, **kwargs):
        request.headers['Host'] = self.target.authority


def _configured_public_http_proxy_url() -> str | None:
    proxy_url = str(
        getattr(settings, 'PUBLIC_HTTP_PROXY_URL', '') or '',
    ).strip()
    if not proxy_url:
        return None
    if proxy_url != TRUSTED_PUBLIC_HTTP_PROXY_URL:
        raise UnsafePublicURL('Разрешён только доверенный public HTTP proxy.')
    return proxy_url


@contextmanager
def _open_pinned_response(
    target: ResolvedPublicTarget,
    *,
    method: str,
    timeout: float | tuple[float, float],
    headers: Mapping[str, str] | None,
    data,
    json_body,
    params,
    auth,
):
    proxy_url = _configured_public_http_proxy_url()
    session = requests.Session()
    session.trust_env = False
    session.adapters.clear()
    adapter: HTTPAdapter
    if proxy_url is None:
        adapter = _PinnedIPAdapter(target)
        request_proxies = {}
    else:
        adapter = _TrustedProxyAdapter(target, proxy_url)
        request_proxies = {target.scheme: proxy_url}
    session.mount(f'{target.scheme}://', adapter)
    request_headers = {
        str(key): str(value)
        for key, value in (headers or {}).items()
        if str(key).lower() != 'accept-encoding'
    }
    # ``requests`` otherwise negotiates compression and transparently expands
    # it while iterating. Requiring identity keeps the byte budget meaningful
    # even for a hostile or compromised public endpoint.
    request_headers['Accept-Encoding'] = 'identity'
    response = None
    try:
        response = session.request(
            method,
            target.url,
            timeout=timeout,
            headers=request_headers,
            data=data,
            json=json_body,
            params=params,
            auth=auth,
            stream=True,
            allow_redirects=False,
            verify=True,
            proxies=request_proxies,
        )
        yield response
    finally:
        if response is not None:
            response.close()
        session.close()


def _headers_for_cross_origin(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Keep only an explicit non-credential allowlist on a new origin."""
    return {
        key: value
        for key, value in (headers or {}).items()
        if key.lower() in SAFE_CROSS_ORIGIN_HEADERS
    }


def _freeze_response_body(response, payload: bytes) -> None:
    response._content = payload
    response._content_consumed = True


def request_public_http_url(
    value: str,
    *,
    method: str = 'GET',
    timeout: float | tuple[float, float],
    headers: Mapping[str, str] | None = None,
    data=None,
    json_body=None,
    params=None,
    auth=None,
    max_response_bytes: int | None = None,
    status_only: bool = False,
    redirect_policy: str = REDIRECT_PUBLIC,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    max_elapsed_seconds: float = DEFAULT_MAX_ELAPSED_SECONDS,
    resolver: Callable | None = None,
):
    """Perform a public request and return a closed bounded response.

    Development connects to the admitted DNS result directly. Production first
    admits only public DNS answers and then uses the exact trusted proxy, whose
    ``dst`` ACL independently resolves and rejects non-public final addresses.
    System/environment proxies remain disabled in both modes. The elapsed budget
    is a hard total across DNS, connect/TLS, response headers, redirects and body
    streaming; per-socket ``requests`` timeouts are secondary.
    """
    normalized_method = str(method or '').upper()
    if normalized_method not in {'GET', 'HEAD', 'POST'}:
        raise ValueError('DNS-pinned transport supports only GET, HEAD and POST.')
    if data is not None and json_body is not None:
        raise ValueError('Нельзя одновременно передавать data и json_body.')
    if normalized_method in {'GET', 'HEAD'} and (data is not None or json_body is not None):
        raise ValueError('Тело запроса разрешено только для POST.')
    if redirect_policy not in REDIRECT_POLICIES:
        raise ValueError('Некорректная redirect policy.')
    if max_redirects < 0:
        raise ValueError('max_redirects должен быть неотрицательным.')
    elapsed_limit = float(max_elapsed_seconds)
    if not math.isfinite(elapsed_limit) or elapsed_limit <= 0:
        raise ValueError('max_elapsed_seconds должен быть положительным и конечным.')
    if not status_only and (
        not isinstance(max_response_bytes, int) or max_response_bytes <= 0
    ):
        raise ValueError('Для ответа обязателен положительный byte budget.')

    deadline = time.monotonic() + elapsed_limit
    try:
        return run_with_deadline(
            lambda: _request_public_http_url_sync(
                value,
                method=normalized_method,
                timeout=timeout,
                headers=headers,
                data=data,
                json_body=json_body,
                params=params,
                auth=auth,
                max_response_bytes=max_response_bytes,
                status_only=status_only,
                redirect_policy=redirect_policy,
                max_redirects=max_redirects,
                resolver=resolver,
                deadline=deadline,
            ),
            deadline=deadline,
        )
    except HTTPDeadlineExceeded as exc:
        raise RequestDeadlineExceeded(
            'HTTP-запрос превысил общий лимит времени.',
        ) from exc


def _request_public_http_url_sync(
    value: str,
    *,
    method: str,
    timeout: float | tuple[float, float],
    headers: Mapping[str, str] | None,
    data,
    json_body,
    params,
    auth,
    max_response_bytes: int | None,
    status_only: bool,
    redirect_policy: str,
    max_redirects: int,
    resolver: Callable | None,
    deadline: float,
):
    """Perform all blocking transport phases inside the bounded worker."""
    current_url = str(value or '').strip()
    current_headers = dict(headers) if headers else {}
    current_auth = auth
    current_params = params

    for redirect_count in range(max_redirects + 1):
        target = resolve_public_http_url(current_url, resolver=resolver)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RequestDeadlineExceeded('HTTP-запрос превысил общий лимит времени.')
        if target.scheme != 'https' and (
            current_auth is not None
            or any(
                str(header_name).lower() in SENSITIVE_TRANSPORT_HEADERS
                for header_name in current_headers
            )
        ):
            raise UnsafePublicURL(
                'Credentials разрешено отправлять только по HTTPS.',
            )
        with _open_pinned_response(
            target,
            method=method,
            timeout=timeout,
            headers=current_headers,
            data=data,
            json_body=json_body,
            params=current_params,
            auth=current_auth,
        ) as response:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RequestDeadlineExceeded('HTTP-запрос превысил общий лимит времени.')
            try:
                with enforce_response_deadline(response, remaining):
                    current_params = None
                    if response.status_code in REDIRECT_STATUS_CODES:
                        location = response.headers.get('Location')
                        if not location:
                            raise UnsafePublicURL('Redirect не содержит допустимого Location.')
                        if redirect_policy == REDIRECT_NONE:
                            raise UnsafePublicURL('HTTP redirect запрещён для этого запроса.')
                        if method not in {'GET', 'HEAD'}:
                            raise UnsafePublicURL('Redirect небезопасен для non-idempotent запроса.')
                        if redirect_count >= max_redirects:
                            raise UnsafePublicURL('Превышен лимит HTTP redirect.')

                        next_url = urljoin(str(response.url or target.url), location)
                        next_parsed = _parse_public_http_url(next_url)
                        if target.scheme == 'https' and next_parsed.scheme != 'https':
                            raise UnsafePublicURL('HTTPS downgrade redirect запрещён.')
                        if (
                            redirect_policy == REDIRECT_SAME_ORIGIN
                            and next_parsed.origin != target.origin
                        ):
                            raise UnsafePublicURL('Redirect на другой origin запрещён.')
                        if next_parsed.origin != target.origin:
                            current_auth = None
                            current_headers = _headers_for_cross_origin(current_headers)
                        current_url = next_parsed.url
                        continue

                    if status_only:
                        payload = b''
                    else:
                        # The public entrypoint validates this before entering
                        # the blocking worker. Keep the internal boundary
                        # defensive and explicit for direct callers and typing.
                        if max_response_bytes is None:
                            raise ValueError(
                                'Для ответа обязателен положительный byte budget.',
                            )
                        payload = read_response_limited(
                            response,
                            max_bytes=max_response_bytes,
                        )
                    _freeze_response_body(response, payload)
                    return response
            except HTTPDeadlineExceeded as exc:
                raise RequestDeadlineExceeded(
                    'HTTP-запрос превысил общий лимит времени.',
                ) from exc

    raise UnsafePublicURL('Превышен лимит HTTP redirect.')


def read_response_limited(
    response,
    *,
    max_bytes: int,
    chunk_size: int = 64 * 1024,
) -> bytes:
    """Read a streamed, decoded response without unbounded memory growth."""
    content_encoding = str(response.headers.get('Content-Encoding', '')).strip().lower()
    if content_encoding not in {'', 'identity'}:
        raise ResponseTooLarge('Сжатые HTTP-ответы запрещены политикой размера.')

    raw_content_length = response.headers.get('Content-Length')
    if raw_content_length:
        try:
            content_length = int(raw_content_length)
        except (TypeError, ValueError) as exc:
            raise ResponseTooLarge('Некорректный Content-Length.') from exc
        if content_length < 0 or content_length > max_bytes:
            raise ResponseTooLarge('Ответ превышает допустимый размер.')

    payload = bytearray()
    if hasattr(response, 'iter_content'):
        chunks = response.iter_content(chunk_size=chunk_size)
    else:
        chunks = response.iter_bytes(chunk_size=chunk_size)
    for chunk in chunks:
        if not chunk:
            continue
        if len(payload) + len(chunk) > max_bytes:
            raise ResponseTooLarge('Ответ превышает допустимый размер.')
        payload.extend(chunk)
    return bytes(payload)
