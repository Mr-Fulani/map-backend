import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')
DEBUG = False
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', '').split(',')

DJANGO_APPS = [
    'unfold',
    'unfold.contrib.filters',
    'unfold.contrib.forms',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.postgres',
]

THIRD_PARTY_APPS = [
    'rest_framework',
    'drf_spectacular',
    'django_celery_beat',
    'storages',
]

LOCAL_APPS = [
    'apps.core',
    'apps.users',
    'apps.tenants',
    'apps.billing',
    'apps.datasources',
    'apps.products',
    'apps.marketplaces',
    'apps.ai_agent',
    'apps.anti_ban',
    'apps.sync',
    'apps.notifications',
    'apps.api',
    'apps.analytics',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

UNFOLD = {
    "SIDEBAR": {
        "navigation": [
            {
                "title": "Аналитика",
                "items": [
                    {
                        "title": "Статистика платформы",
                        "icon": "bar_chart",
                        "link": "/admin/stats/",
                    },
                ],
            },
            {
                "title": "Пользователи и тенанты",
                "collapsible": True,
                "items": [
                    {"title": "Тенанты", "icon": "domain", "link": "/admin/tenants/tenant/"},
                    {"title": "Пользователи", "icon": "person", "link": "/admin/users/user/"},
                    {"title": "API-ключи", "icon": "key", "link": "/admin/tenants/apikey/"},
                ],
            },
            {
                "title": "Биллинг",
                "collapsible": True,
                "items": [
                    {"title": "Тарифные планы", "icon": "sell", "link": "/admin/billing/plan/"},
                    {"title": "Подписки", "icon": "subscriptions", "link": "/admin/billing/subscription/"},
                    {"title": "Счета", "icon": "receipt", "link": "/admin/billing/invoice/"},
                ],
            },
            {
                "title": "Товары и маркетплейсы",
                "collapsible": True,
                "items": [
                    {"title": "Товары", "icon": "inventory_2", "link": "/admin/products/product/"},
                    {"title": "Листинги", "icon": "storefront", "link": "/admin/marketplaces/listing/"},
                    {"title": "Аккаунты", "icon": "manage_accounts", "link": "/admin/marketplaces/marketplaceaccount/"},
                    {"title": "Источники данных", "icon": "database", "link": "/admin/datasources/datasourceconnection/"},
                ],
            },
            {
                "title": "Система",
                "collapsible": True,
                "items": [
                    {"title": "Логи синхронизации", "icon": "sync", "link": "/admin/sync/synclog/"},
                    {"title": "Уведомления", "icon": "notifications", "link": "/admin/notifications/notificationlog/"},
                    {"title": "Настройки уведомлений", "icon": "settings", "link": "/admin/notifications/tenantnotificationsettings/"},
                    {"title": "Периодические задачи", "icon": "schedule", "link": "/admin/django_celery_beat/periodictask/"},
                ],
            },
        ],
    },
}

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'apps.core.middleware.TenantMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# --- База данных ---
DATABASES = {
    'default': dj_database_url.config(
        env='DATABASE_URL',
        conn_max_age=600,
        conn_health_checks=True,
    )
}

# --- Redis / Cache ---
REDIS_URL = os.environ.get('REDIS_URL', 'redis://redis:6379/0')

CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': REDIS_URL,
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
        },
        'KEY_PREFIX': 'map',
    }
}

# --- Celery ---
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'Europe/Moscow'
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'
CELERY_BEAT_SCHEDULE = {
    'cleanup-old-logs-daily': {
        'task': 'apps.notifications.tasks.cleanup_old_logs',
        'schedule': 60 * 60 * 24,  # раз в сутки
        'options': {'queue': 'notifications'},
    },
}
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 3600
CELERY_TASK_SOFT_TIME_LIMIT = 3300

# Очереди и их приоритеты
CELERY_TASK_QUEUES = {
    'sync_import':   {'exchange': 'sync_import',   'routing_key': 'sync_import'},
    'avito_publish': {'exchange': 'avito_publish', 'routing_key': 'avito_publish'},
    'avito_update':  {'exchange': 'avito_update',  'routing_key': 'avito_update'},
    'avito_price':   {'exchange': 'avito_price',   'routing_key': 'avito_price'},
    'avito_delete':  {'exchange': 'avito_delete',  'routing_key': 'avito_delete'},
    'ai_generate':   {'exchange': 'ai_generate',   'routing_key': 'ai_generate'},
    'notifications': {'exchange': 'notifications', 'routing_key': 'notifications'},
    'billing':       {'exchange': 'billing',       'routing_key': 'billing'},
}
CELERY_TASK_DEFAULT_QUEUE = 'sync_import'

# --- DRF ---
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'apps.tenants.authentication.APIKeyAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'apps.core.pagination.MapPagination',
    'PAGE_SIZE': 50,
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'EXCEPTION_HANDLER': 'apps.core.exceptions.map_exception_handler',
}

# --- Swagger ---
SPECTACULAR_SETTINGS = {
    'TITLE': 'MAP API',
    'DESCRIPTION': 'Marketplace Automation Platform — API для автоматизации объявлений',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
}

# --- Файловое хранилище (Yandex Cloud S3) ---
YC_S3_BUCKET = os.environ.get('YC_S3_BUCKET', '')
YC_S3_ACCESS_KEY = os.environ.get('YC_S3_ACCESS_KEY', '')
YC_S3_SECRET_KEY = os.environ.get('YC_S3_SECRET_KEY', '')
YC_CDN_DOMAIN = os.environ.get('YC_CDN_DOMAIN', '')

if YC_S3_BUCKET:
    STORAGES = {
        'default': {
            'BACKEND': 'storages.backends.s3boto3.S3Boto3Storage',
            'OPTIONS': {
                'endpoint_url': 'https://storage.yandexcloud.net',
                'region_name': 'ru-central1',
                'bucket_name': YC_S3_BUCKET,
                'access_key': YC_S3_ACCESS_KEY,
                'secret_key': YC_S3_SECRET_KEY,
                'file_overwrite': False,
                'default_acl': 'private',
                'custom_domain': YC_CDN_DOMAIN or None,
            },
        },
        'staticfiles': {
            'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
        },
    }

# --- Безопасность ---
FIELD_ENCRYPTION_KEY = os.environ.get('FIELD_ENCRYPTION_KEY', '')
WEBHOOK_SIGNING_SECRET = os.environ.get('WEBHOOK_SIGNING_SECRET', '')

# --- AI ---
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')

# --- Avito ---
AVITO_CLIENT_ID = os.environ.get('AVITO_CLIENT_ID', '')
AVITO_CLIENT_SECRET = os.environ.get('AVITO_CLIENT_SECRET', '')

# --- Биллинг ---
YOOKASSA_SHOP_ID = os.environ.get('YOOKASSA_SHOP_ID', '')
YOOKASSA_SECRET_KEY = os.environ.get('YOOKASSA_SECRET_KEY', '')
SITE_URL = os.environ.get('SITE_URL', 'http://localhost:8000')

# --- Уведомления ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'noreply@yourdomain.ru')
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.environ.get('SENDPULSE_SMTP_HOST', 'smtp.sendpulse.com')
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.environ.get('SENDPULSE_SMTP_LOGIN', '')
EMAIL_HOST_PASSWORD = os.environ.get('SENDPULSE_SMTP_PASSWORD', '')

# --- Пользователь ---
AUTH_USER_MODEL = 'users.User'

# --- Локализация ---
LANGUAGE_CODE = 'ru-ru'
TIME_ZONE = 'Europe/Moscow'
USE_I18N = True
USE_TZ = True

# --- Статика / Медиа ---
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- Пароли ---
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
]
