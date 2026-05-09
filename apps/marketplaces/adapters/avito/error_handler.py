from apps.marketplaces.adapters.avito.rate_limiter import RateLimitError

# Расписание задержек при повторных попытках: 30s → 60s → 120s → 300s
BACKOFF_SCHEDULE = [30, 60, 120, 300]


class AvitoError(Exception):
    """Базовый класс ошибок Avito API."""


class TokenExpiredError(AvitoError):
    """Токен истёк (HTTP 401) — нужно обновить и повторить."""


class ForbiddenError(AvitoError):
    """Нет прав на операцию (HTTP 403) — требует ручного вмешательства."""


class NotFoundError(AvitoError):
    """Объявление не найдено (HTTP 404) — нужно переопубликовать."""


class DuplicateError(AvitoError):
    """Конфликт дублирования (HTTP 409) — проверить idempotency_key."""


class PhotoTooLargeError(AvitoError):
    """Фото превышает допустимый размер (HTTP 413) — уменьшить до 1280px."""


class RejectedError(AvitoError):
    """Объявление отклонено модератором (HTTP 422)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ServerError(AvitoError):
    """Ошибка сервера Avito (5xx) — retry с backoff."""


def handle_avito_error(response, listing=None):
    """Преобразует HTTP-ответ Avito в соответствующее исключение."""
    code = response.status_code

    if code == 400:
        raise AvitoError(f'Невалидные данные: {response.text[:200]}')
    elif code == 401:
        raise TokenExpiredError('Токен истёк')
    elif code == 403:
        raise ForbiddenError('Нет прав доступа')
    elif code == 404:
        raise NotFoundError('Объявление не найдено')
    elif code == 409:
        raise DuplicateError('Дублирование объявления')
    elif code == 413:
        raise PhotoTooLargeError('Фото слишком большое')
    elif code == 422:
        try:
            reason = response.json().get('error', {}).get('message', response.text[:200])
        except Exception:
            reason = response.text[:200]
        raise RejectedError(reason)
    elif code == 429:
        retry_after = int(response.headers.get('Retry-After', 30))
        raise RateLimitError(retry_after=retry_after)
    elif code >= 500:
        raise ServerError(f'Ошибка сервера Avito: {code}')

    response.raise_for_status()


def backoff(retry_count: int) -> int:
    """Возвращает задержку в секундах по номеру попытки (экспоненциальный backoff)."""
    idx = min(retry_count, len(BACKOFF_SCHEDULE) - 1)
    return BACKOFF_SCHEDULE[idx]
