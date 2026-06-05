import os

import dj_database_url

from .base import *  # noqa: F401, F403

DEBUG = True
SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-dev-key-not-for-production-use-only'
)

ALLOWED_HOSTS = ['*']
MEDIA_KEY_PREFIX = os.environ.get('MEDIA_KEY_PREFIX', 'dev').strip('/')

DATABASES = {
    'default': dj_database_url.config(
        default='postgres://map_user:map_password@db:5432/map_db',
        conn_max_age=600,
        conn_health_checks=True,
    )
}

# Упрощённое логирование в dev
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {'class': 'logging.StreamHandler'},
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'celery': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'apps': {'handlers': ['console'], 'level': 'DEBUG', 'propagate': False},
    },
}

if not YC_S3_BUCKET:
    # В dev без S3 используем локальное хранилище.
    STORAGES = {
        'default': {
            'BACKEND': 'django.core.files.storage.FileSystemStorage',
        },
        'staticfiles': {
            'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
        },
    }

EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
