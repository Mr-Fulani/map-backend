import os
from urllib.parse import urlsplit

import sentry_sdk
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration

from config.redis_config import validate_production_redis_layout
from config.sentry_scrubbing import scrub_sentry_breadcrumb, scrub_sentry_event

from .base import *  # noqa: F401, F403

DEBUG = False


def _required_env(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value:
        raise ImproperlyConfigured(f'{name} обязателен в production.')
    return value


SECRET_KEY = _required_env('DJANGO_SECRET_KEY')
if len(SECRET_KEY) < 50 or 'insecure' in SECRET_KEY.lower():
    raise ImproperlyConfigured('DJANGO_SECRET_KEY должен быть случайным и не короче 50 символов.')

ALLOWED_HOSTS = [host.strip() for host in _required_env('ALLOWED_HOSTS').split(',') if host.strip()]
if '*' in ALLOWED_HOSTS:
    raise ImproperlyConfigured('Wildcard ALLOWED_HOSTS запрещён в production.')

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in _required_env('CSRF_TRUSTED_ORIGINS').split(',')
    if origin.strip()
]

database_url = _required_env('DATABASE_URL')
if 'map_password' in database_url:
    raise ImproperlyConfigured('Dev-пароль БД запрещён в production DATABASE_URL.')

cache_redis_url = _required_env('CACHE_REDIS_URL')
celery_broker_url = _required_env('CELERY_BROKER_URL')
celery_result_backend = _required_env('CELERY_RESULT_BACKEND')
coordination_redis_url = _required_env('COORDINATION_REDIS_URL')
try:
    validate_production_redis_layout(
        cache_redis_url,
        celery_broker_url,
        celery_result_backend,
        coordination_redis_url,
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

if not SITE_URL.startswith('https://') or not FRONTEND_URL.startswith('https://'):
    raise ImproperlyConfigured('SITE_URL и FRONTEND_URL должны использовать HTTPS в production.')


def _is_https_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == 'https'
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and parsed.path in ('', '/')
        and not parsed.query
        and not parsed.fragment
    )


if not BILLING_RETURN_URL_ALLOWED_ORIGINS or not all(
    _is_https_origin(origin)
    for origin in BILLING_RETURN_URL_ALLOWED_ORIGINS
):
    raise ImproperlyConfigured(
        'BILLING_RETURN_URL_ALLOWED_ORIGINS должен содержать только HTTPS origins.',
    )
if YOOKASSA_ALLOW_TEST_PAYMENTS:
    raise ImproperlyConfigured('YOOKASSA_ALLOW_TEST_PAYMENTS запрещён в production.')
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
