import os
from urllib.parse import urlparse

import sentry_sdk
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration

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

redis_url = _required_env('REDIS_URL')
if not urlparse(redis_url).password:
    raise ImproperlyConfigured('Production REDIS_URL должен содержать пароль.')

if not FIELD_ENCRYPTION_KEYS:
    raise ImproperlyConfigured('FIELD_ENCRYPTION_KEYS или FIELD_ENCRYPTION_KEY обязателен.')
try:
    for encryption_key in FIELD_ENCRYPTION_KEYS:
        Fernet(encryption_key.encode())
except (TypeError, ValueError) as exc:
    raise ImproperlyConfigured('Некорректный Fernet-ключ FIELD_ENCRYPTION_KEYS.') from exc

if not SITE_URL.startswith('https://') or not FRONTEND_URL.startswith('https://'):
    raise ImproperlyConfigured('SITE_URL и FRONTEND_URL должны использовать HTTPS в production.')

# HTTPS / Security headers
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [r'^api/v1/health/$']
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
        before_send=lambda event, hint: _scrub_secrets(event),
    )


def _scrub_secrets(event: dict) -> dict:
    """Очищает API-ключи и токены из Sentry-событий перед отправкой."""
    sensitive_keys = {'password', 'token', 'secret', 'key', 'authorization'}
    if 'request' in event and 'headers' in event['request']:
        headers = event['request']['headers']
        for key in list(headers):
            if any(s in key.lower() for s in sensitive_keys):
                headers[key] = '[Filtered]'
    return event


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
