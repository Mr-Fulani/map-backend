import os
from urllib.parse import urlsplit

import sentry_sdk
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import EmailValidator, URLValidator
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration

from config.redis_config import validate_production_redis_layout
from config.sentry_scrubbing import scrub_sentry_breadcrumb, scrub_sentry_event

from .base import *  # noqa: F401, F403

DEBUG = False
_https_url_validator = URLValidator(schemes=['https'])
_email_validator = EmailValidator()


def _required_env(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value:
        raise ImproperlyConfigured(f'{name} обязателен в production.')
    return value


def _required_bool_env(name: str) -> bool:
    value = _required_env(name).lower()
    if value not in {'true', 'false'}:
        raise ImproperlyConfigured(
            f'{name} должен быть явно задан как true или false.',
        )
    return value == 'true'


def _is_https_origin(value: str) -> bool:
    if any(character.isspace() for character in value):
        return False
    try:
        _https_url_validator(value)
        parsed = urlsplit(value)
        parsed.port
    except (ValidationError, ValueError):
        return False
    return bool(
        parsed.scheme == 'https'
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ''
        and not parsed.query
        and not parsed.fragment
        and not parsed.netloc.endswith(':')
    )


def _required_https_origin(name: str) -> str:
    value = _required_env(name)
    if not _is_https_origin(value):
        raise ImproperlyConfigured(
            f'{name} должен быть чистым HTTPS origin без path/query/fragment.',
        )
    return value


def _required_https_origins(name: str) -> list[str]:
    values = [value.strip() for value in _required_env(name).split(',')]
    if any(not value or not _is_https_origin(value) for value in values):
        raise ImproperlyConfigured(
            f'{name} должен содержать только чистые HTTPS origins.',
        )
    return values


SECRET_KEY = _required_env('DJANGO_SECRET_KEY')
if len(SECRET_KEY) < 50 or 'insecure' in SECRET_KEY.lower():
    raise ImproperlyConfigured('DJANGO_SECRET_KEY должен быть случайным и не короче 50 символов.')

ALLOWED_HOSTS = [host.strip() for host in _required_env('ALLOWED_HOSTS').split(',') if host.strip()]
if '*' in ALLOWED_HOSTS:
    raise ImproperlyConfigured('Wildcard ALLOWED_HOSTS запрещён в production.')

CORS_ALLOWED_ORIGINS = _required_https_origins('CORS_ALLOWED_ORIGINS')
CSRF_TRUSTED_ORIGINS = _required_https_origins('CSRF_TRUSTED_ORIGINS')
SITE_URL = _required_https_origin('SITE_URL')
FRONTEND_URL = _required_https_origin('FRONTEND_URL')
if PUBLIC_HTTP_PREFLIGHT_URL != 'https://storage.yandexcloud.net/':
    raise ImproperlyConfigured(
        'PUBLIC_HTTP_PREFLIGHT_URL должен указывать на фиксированный '
        'Yandex Object Storage infrastructure endpoint.',
    )
site_hostname = urlsplit(SITE_URL).hostname
if not site_hostname or not any(
    allowed_host == site_hostname
    or (
        allowed_host.startswith('.')
        and (
            site_hostname == allowed_host[1:]
            or site_hostname.endswith(allowed_host)
        )
    )
    for allowed_host in ALLOWED_HOSTS
):
    raise ImproperlyConfigured(
        'Hostname из SITE_URL должен быть разрешён в ALLOWED_HOSTS.',
    )

EMAIL_BACKEND = 'apps.core.email_backend.HTTPProxySMTPEmailBackend'
EMAIL_HOST = 'smtp.resend.com'
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_USE_SSL = False
EMAIL_TIMEOUT = 10
EMAIL_HOST_USER = 'resend'
EMAIL_HOST_PASSWORD = _required_env('RESEND_API_KEY')
if not EMAIL_HOST_PASSWORD.startswith('re_') or len(EMAIL_HOST_PASSWORD) < 20:
    raise ImproperlyConfigured('RESEND_API_KEY должен быть ключом Resend.')
DEFAULT_FROM_EMAIL = _required_env('DEFAULT_FROM_EMAIL')
try:
    _email_validator(DEFAULT_FROM_EMAIL)
except ValidationError as exc:
    raise ImproperlyConfigured(
        'DEFAULT_FROM_EMAIL должен быть корректным email без display name.',
    ) from exc
if DEFAULT_FROM_EMAIL.rsplit('@', 1)[-1].lower() != 'notify.dodugir.com':
    raise ImproperlyConfigured(
        'DEFAULT_FROM_EMAIL должен принадлежать platform-домену notify.dodugir.com.',
    )
EMAIL_HTTP_PROXY_URL = _required_env('EMAIL_HTTP_PROXY_URL')
if EMAIL_HTTP_PROXY_URL != 'http://egress_proxy:3128':
    raise ImproperlyConfigured(
        'EMAIL_HTTP_PROXY_URL в production должен быть http://egress_proxy:3128.',
    )
PUBLIC_HTTP_PROXY_URL = _required_env('PUBLIC_HTTP_PROXY_URL')
if PUBLIC_HTTP_PROXY_URL != 'http://egress_proxy:3128':
    raise ImproperlyConfigured(
        'PUBLIC_HTTP_PROXY_URL в production должен быть http://egress_proxy:3128.',
    )

database_url = _required_env('DATABASE_URL')
if 'map_password' in database_url:
    raise ImproperlyConfigured('Dev-пароль БД запрещён в production DATABASE_URL.')

# Production containers run with a read-only root filesystem. Local media
# fallback would therefore pass health checks but fail every upload at runtime.
YC_S3_BUCKET = _required_env('YC_S3_BUCKET')
YC_S3_ACCESS_KEY = _required_env('YC_S3_ACCESS_KEY')
YC_S3_SECRET_KEY = _required_env('YC_S3_SECRET_KEY')

cache_redis_url = _required_env('CACHE_REDIS_URL')
celery_broker_url = _required_env('CELERY_BROKER_URL')
celery_result_backend = _required_env('CELERY_RESULT_BACKEND')
coordination_redis_url = _required_env('COORDINATION_REDIS_URL')
cache_redis_password = _required_env('CACHE_REDIS_PASSWORD')
celery_redis_password = _required_env('CELERY_REDIS_PASSWORD')
try:
    validate_production_redis_layout(
        cache_redis_url,
        celery_broker_url,
        celery_result_backend,
        coordination_redis_url,
        cache_server_password=cache_redis_password,
        broker_server_password=celery_redis_password,
    )
except ValueError as exc:
    raise ImproperlyConfigured(str(exc)) from exc

if not FIELD_ENCRYPTION_KEYS:
    raise ImproperlyConfigured('FIELD_ENCRYPTION_KEYS или FIELD_ENCRYPTION_KEY обязателен.')
try:
    for encryption_key in FIELD_ENCRYPTION_KEYS:
        Fernet(encryption_key.encode())
except (TypeError, ValueError) as exc:
    raise ImproperlyConfigured('Некорректный Fernet-ключ FIELD_ENCRYPTION_KEYS.') from exc

if not BILLING_RETURN_URL_ALLOWED_ORIGINS or not all(
    _is_https_origin(origin)
    for origin in BILLING_RETURN_URL_ALLOWED_ORIGINS
):
    raise ImproperlyConfigured(
        'BILLING_RETURN_URL_ALLOWED_ORIGINS должен содержать только HTTPS origins.',
    )
for origin_setting_name, allowed_origins in (
    ('CORS_ALLOWED_ORIGINS', CORS_ALLOWED_ORIGINS),
    ('CSRF_TRUSTED_ORIGINS', CSRF_TRUSTED_ORIGINS),
    ('BILLING_RETURN_URL_ALLOWED_ORIGINS', BILLING_RETURN_URL_ALLOWED_ORIGINS),
):
    if FRONTEND_URL not in allowed_origins:
        raise ImproperlyConfigured(
            f'{origin_setting_name} должен включать FRONTEND_URL.',
        )
BILLING_ENABLED = _required_bool_env('BILLING_ENABLED')
if YOOKASSA_ALLOW_TEST_PAYMENTS:
    raise ImproperlyConfigured('YOOKASSA_ALLOW_TEST_PAYMENTS запрещён в production.')
if BILLING_ENABLED:
    YOOKASSA_SHOP_ID = _required_env('YOOKASSA_SHOP_ID')
    YOOKASSA_SECRET_KEY = _required_env('YOOKASSA_SECRET_KEY')
try:
    _yookassa_api_url = urlsplit(YOOKASSA_API_BASE_URL)
    _valid_yookassa_api_url = (
        _yookassa_api_url.scheme == 'https'
        and _yookassa_api_url.hostname == 'api.yookassa.ru'
        and _yookassa_api_url.port in (None, 443)
        and _yookassa_api_url.path.rstrip('/') == '/v3'
        and _yookassa_api_url.username is None
        and _yookassa_api_url.password is None
        and not _yookassa_api_url.query
        and not _yookassa_api_url.fragment
    )
except ValueError:
    _valid_yookassa_api_url = False
if not _valid_yookassa_api_url:
    raise ImproperlyConfigured(
        'YOOKASSA_API_BASE_URL в production должен указывать на '
        'https://api.yookassa.ru/v3.',
    )
if YOOKASSA_API_MAX_RESPONSE_BYTES > 4 * 1024 * 1024:
    raise ImproperlyConfigured(
        'YOOKASSA_API_MAX_RESPONSE_BYTES не должен превышать 4 MiB.',
    )

# HTTPS / Security headers
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [
    r'^api/v1/health/$',
    r'^api/v1/live/$',
    r'^api/v1/ready/$',
]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = 'DENY'
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True

# Sentry
SENTRY_DSN = os.environ.get('SENTRY_DSN', '')
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration(), CeleryIntegration()],
        traces_sample_rate=0.1,
        send_default_pii=False,
        max_request_body_size='never',
        include_local_variables=False,
        before_send=scrub_sentry_event,
        before_breadcrumb=scrub_sentry_breadcrumb,
    )


LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'json': {
            'format': '%(asctime)s %(levelname)s %(name)s %(message)s',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'json',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
    'loggers': {
        'django': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'celery': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'apps': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
    },
}
