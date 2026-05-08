from apps.marketplaces.adapters.avito.rate_limiter import RateLimitError

BACKOFF_SCHEDULE = [30, 60, 120, 300]


class AvitoError(Exception):
    pass


class TokenExpiredError(AvitoError):
    pass


class ForbiddenError(AvitoError):
    pass


class NotFoundError(AvitoError):
    pass


class DuplicateError(AvitoError):
    pass


class PhotoTooLargeError(AvitoError):
    pass


class RejectedError(AvitoError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ServerError(AvitoError):
    pass


def handle_avito_error(response, listing=None):
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
    idx = min(retry_count, len(BACKOFF_SCHEDULE) - 1)
    return BACKOFF_SCHEDULE[idx]
