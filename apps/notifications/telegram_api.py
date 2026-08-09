"""Small, bounded client helpers for Telegram Bot API control calls."""

from collections.abc import Callable

import requests

from apps.core.http_responses import (
    TrustedResponseError,
    bounded_http_request,
)


class TelegramAPIError(RuntimeError):
    """Telegram could not provide a valid successful API response."""


def request_telegram_json(
    requester: Callable,
    url: str,
    *,
    timeout: tuple[float, float],
    max_elapsed_seconds: float,
    max_bytes: int,
    **kwargs,
) -> dict:
    """Return a successful Telegram JSON envelope without leaking bot URLs."""
    try:
        response = bounded_http_request(
            requester,
            url,
            timeout=timeout,
            allow_redirects=False,
            max_elapsed_seconds=max_elapsed_seconds,
            max_bytes=max_bytes,
            **kwargs,
        )
    except (requests.RequestException, TrustedResponseError) as exc:
        # requests exceptions can include the request URL, which contains the
        # bot token. Keep the operator-facing error useful but secret-free.
        raise TelegramAPIError(
            f'Telegram API transport failed ({type(exc).__name__}).'
        ) from None

    status_code = getattr(response, 'status_code', None)
    if (
        not isinstance(status_code, int)
        or isinstance(status_code, bool)
        or not 200 <= status_code < 300
    ):
        safe_status = status_code if isinstance(status_code, int) else 'unknown'
        raise TelegramAPIError(f'Telegram API returned HTTP {safe_status}.')

    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise TelegramAPIError('Telegram API returned invalid JSON.') from exc

    if not isinstance(payload, dict) or type(payload.get('ok')) is not bool:
        raise TelegramAPIError('Telegram API returned an invalid JSON envelope.')

    if payload['ok'] is False:
        description = payload.get('description')
        error_code = payload.get('error_code')
        if (
            not isinstance(error_code, int)
            or isinstance(error_code, bool)
            or not isinstance(description, str)
        ):
            raise TelegramAPIError('Telegram API returned an invalid error envelope.')
        detail = description[:200] or 'request rejected'
        raise TelegramAPIError(f'Telegram API rejected the request: {detail}.')

    if 'result' not in payload:
        raise TelegramAPIError('Telegram API response has no result.')
    return payload


def expect_boolean_result(payload: dict) -> None:
    """Require the boolean ``result`` used by webhook mutation methods."""
    if payload.get('result') is not True:
        raise TelegramAPIError('Telegram API returned an invalid boolean result.')
