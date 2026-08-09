"""Bounded buffering for responses from trusted, fixed-origin HTTP APIs.

User-controlled URLs need DNS pinning and redirect controls as well; those
callers must use :mod:`apps.core.url_security` instead.
"""

from collections.abc import Callable, Mapping
import math
import time

from apps.core.http_deadlines import (
    HTTPDeadlineExceeded,
    enforce_response_deadline,
    run_with_deadline,
)


CHUNK_SIZE = 64 * 1024
DEFAULT_MAX_ELAPSED_SECONDS = 60.0


class TrustedResponseError(ValueError):
    """A fixed-origin API response violated the bounded-response policy."""


class TrustedResponseTooLarge(TrustedResponseError):
    """A trusted API returned more bytes than the configured hard limit."""


class UnsupportedResponseEncoding(TrustedResponseError):
    """A trusted API ignored the identity-only response encoding policy."""


class TrustedResponseDeadlineExceeded(TrustedResponseError):
    """A trusted API response exceeded the total wall-clock budget."""


def bounded_http_request(
    requester: Callable,
    *args,
    max_bytes: int,
    max_elapsed_seconds: float = DEFAULT_MAX_ELAPSED_SECONDS,
    **kwargs,
):
    """Execute a fixed-origin request and return a closed, bounded response.

    ``requests`` normally buffers the complete response before returning.  We
    force streaming, reject compressed bodies, and only then populate the
    response's normal ``content``/``text``/``json`` interface.  Keeping that
    interface also keeps existing provider error handling and tests simple.
    One absolute deadline covers the requester itself (DNS, connect/TLS and
    response headers) as well as streamed body reads.
    """
    limit = int(max_bytes)
    if limit < 1:
        raise ValueError('max_bytes must be positive')
    elapsed_limit = float(max_elapsed_seconds)
    if not math.isfinite(elapsed_limit) or elapsed_limit <= 0:
        raise ValueError('max_elapsed_seconds must be finite and positive')

    headers = {
        str(key): str(value)
        for key, value in dict(kwargs.pop('headers', {}) or {}).items()
        if str(key).lower() != 'accept-encoding'
    }
    headers['Accept-Encoding'] = 'identity'
    kwargs['headers'] = headers
    kwargs['stream'] = True
    # Provider credentials must never follow a redirect to another origin.
    # Fixed API integrations should update their explicit endpoint instead.
    kwargs['allow_redirects'] = False

    deadline = time.monotonic() + elapsed_limit
    try:
        return run_with_deadline(
            lambda: _bounded_http_request_sync(
                requester,
                args,
                kwargs,
                limit=limit,
                deadline=deadline,
            ),
            deadline=deadline,
        )
    except HTTPDeadlineExceeded as exc:
        raise TrustedResponseDeadlineExceeded(
            'Remote response exceeded its wall-clock deadline.',
        ) from exc


def _bounded_http_request_sync(
    requester: Callable,
    args: tuple,
    kwargs: dict,
    *,
    limit: int,
    deadline: float,
):
    """Execute and buffer inside the bounded deadline worker."""
    response = requester(*args, **kwargs)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TrustedResponseDeadlineExceeded(
                'Remote response exceeded its wall-clock deadline.',
            )
        try:
            with enforce_response_deadline(response, remaining):
                response_headers = getattr(response, 'headers', {})
                if not isinstance(response_headers, Mapping):
                    response_headers = {}
                content_encoding = str(response_headers.get('Content-Encoding', '')).strip().lower()
                if content_encoding not in {'', 'identity'}:
                    raise UnsupportedResponseEncoding(
                        f'Unsupported Content-Encoding: {content_encoding}'
                    )

                content_length = str(response_headers.get('Content-Length', '')).strip()
                if content_length:
                    try:
                        declared_length = int(content_length)
                    except (TypeError, ValueError):
                        raise TrustedResponseError(
                            'Remote response has an invalid Content-Length.'
                        ) from None
                    if declared_length < 0:
                        raise TrustedResponseError(
                            'Remote response has a negative Content-Length.'
                        )
                    if declared_length > limit:
                        raise TrustedResponseTooLarge(
                            f'Remote response exceeds the {limit}-byte limit.'
                        )

                chunks = response.iter_content(chunk_size=CHUNK_SIZE, decode_unicode=False)
                try:
                    iterator = iter(chunks)
                except TypeError:
                    # Lightweight response doubles often expose only a configured
                    # .json() method. Real requests.Response objects always return an
                    # iterator here, so the production byte cap cannot take this path.
                    fallback_content = getattr(response, 'content', b'')
                    iterator = iter([fallback_content] if isinstance(fallback_content, bytes) else [])

                body = bytearray()
                for chunk in iterator:
                    if not chunk:
                        continue
                    if isinstance(chunk, str):
                        chunk = chunk.encode(getattr(response, 'encoding', None) or 'utf-8')
                    if len(body) + len(chunk) > limit:
                        raise TrustedResponseTooLarge(
                            f'Remote response exceeds the {limit}-byte limit.'
                        )
                    body.extend(chunk)

                # Populate requests.Response's regular accessors after bounded reading.
                # Duck-typed mocks keep their configured .json() method, while real
                # responses use this exact bounded byte buffer.
                response._content = bytes(body)
                response._content_consumed = True
                return response
        except HTTPDeadlineExceeded as exc:
            raise TrustedResponseDeadlineExceeded(
                'Remote response exceeded its wall-clock deadline.',
            ) from exc
    finally:
        response.close()


def trusted_api_max_bytes(settings_object) -> int:
    """Read the shared trusted-API byte budget without importing Django here."""
    return int(getattr(settings_object, 'TRUSTED_API_RESPONSE_MAX_BYTES', 5 * 1024 * 1024))
