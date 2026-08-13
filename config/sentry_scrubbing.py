"""Fail-closed redaction for error telemetry."""

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sentry_sdk.types import Event


_SENSITIVE_KEY_PARTS = (
    'authorization',
    'cookie',
    'credential',
    'csrf',
    'password',
    'passwd',
    'refresh',
    'secret',
    'token',
)
_SENSITIVE_EXACT_KEYS = {
    'api_key',
    'apikey',
    'confirm_url',
    'key',
    'magic_link',
    'reset_url',
    'verification_url',
}


def _is_sensitive_key(value: object) -> bool:
    key = str(value).strip().lower().replace('-', '_')
    return key in _SENSITIVE_EXACT_KEYS or any(
        fragment in key for fragment in _SENSITIVE_KEY_PARTS
    )


def _without_url_secrets(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return '[Filtered]'
    if not parsed.scheme or not parsed.netloc:
        return '[Filtered]'
    hostname = parsed.hostname
    if not hostname:
        return '[Filtered]'
    try:
        port = parsed.port
    except ValueError:
        return '[Filtered]'
    host = f'[{hostname}]' if ':' in hostname else hostname
    authority = f'{host}:{port}' if port is not None else host
    return urlunsplit((parsed.scheme, authority, parsed.path, '', ''))


def _redact(value, *, parent_key: str = ''):
    if isinstance(value, dict):
        for key in list(value):
            if _is_sensitive_key(key):
                value[key] = '[Filtered]'
            else:
                value[key] = _redact(value[key], parent_key=str(key).lower())
        return value
    if isinstance(value, list):
        return [_redact(item, parent_key=parent_key) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, parent_key=parent_key) for item in value)
    if isinstance(value, str) and parent_key in {'url', 'request_url'}:
        return _without_url_secrets(value)
    return value


def scrub_sentry_event(
    event: Event,
    _hint: dict[str, Any] | None = None,
) -> Event:
    """Remove request secrets even if an integration adds unexpected fields."""
    request = event.get('request')
    if isinstance(request, dict):
        for key in ('data', 'cookies', 'query_string'):
            if key in request:
                request[key] = '[Filtered]'
        request_url = request.get('url')
        if isinstance(request_url, str):
            request['url'] = _without_url_secrets(request_url)
        if '/auth/' in str(request.get('url') or ''):
            event['breadcrumbs'] = {'values': []}
    return _redact(event)


def scrub_sentry_breadcrumb(crumb: dict, _hint=None) -> dict:
    """Strip credentials and URL queries before a breadcrumb is retained."""
    return _redact(crumb)
