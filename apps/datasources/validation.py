"""Validation shared by datasource serializers and runtime adapters."""

from urllib.parse import urlsplit

from apps.core.url_security import is_safe_public_http_url


ONEC_TYPES = frozenset({'1c_http', '1c_xml'})
ONEC_CREDENTIAL_KEYS = frozenset({'url', 'user', 'password'})
MAX_ONEC_URL_LENGTH = 2048
MAX_ONEC_USERNAME_LENGTH = 256
MAX_ONEC_PASSWORD_LENGTH = 2048


class OneCCredentialsValidationError(ValueError):
    pass


def validate_onec_https_url(value: object) -> str:
    """Validate a 1C endpoint without performing DNS/network I/O."""
    if not isinstance(value, str):
        raise OneCCredentialsValidationError('URL источника 1С должен быть строкой.')
    url = value.strip()
    if not url or len(url) > MAX_ONEC_URL_LENGTH:
        raise OneCCredentialsValidationError('Некорректная длина URL источника 1С.')
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise OneCCredentialsValidationError('Некорректный URL источника 1С.') from exc
    if parsed.scheme.lower() != 'https':
        raise OneCCredentialsValidationError(
            'Источник 1С должен использовать HTTPS для защиты учётных данных.',
        )
    if parsed.query or parsed.fragment:
        raise OneCCredentialsValidationError(
            'Query и fragment в URL источника 1С запрещены; используйте отдельные поля учётных данных.',
        )
    if not is_safe_public_http_url(url):
        raise OneCCredentialsValidationError(
            'URL источника 1С должен указывать на допустимый публичный HTTPS endpoint.',
        )
    return url


def validate_onec_credentials(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise OneCCredentialsValidationError('Учётные данные 1С должны быть объектом.')
    if any(not isinstance(key, str) for key in value):
        raise OneCCredentialsValidationError('Названия полей учётных данных должны быть строками.')
    unexpected = sorted(set(value) - ONEC_CREDENTIAL_KEYS)
    if unexpected:
        raise OneCCredentialsValidationError(
            f'Неизвестные поля учётных данных 1С: {", ".join(unexpected)}.',
        )

    url = validate_onec_https_url(value.get('url'))
    user = value.get('user', '')
    password = value.get('password', '')
    if not isinstance(user, str) or len(user) > MAX_ONEC_USERNAME_LENGTH:
        raise OneCCredentialsValidationError('Некорректная длина логина 1С.')
    if not isinstance(password, str) or len(password) > MAX_ONEC_PASSWORD_LENGTH:
        raise OneCCredentialsValidationError('Некорректная длина пароля 1С.')
    return {
        'url': url,
        'user': user,
        'password': password,
    }
