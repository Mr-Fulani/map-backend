import os
from datetime import timedelta
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
    'rest_framework_simplejwt',
    'corsheaders',
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
    'apps.image_search',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

UNFOLD = {
    "SITE_TITLE": "MAP Админ",
    "SITE_HEADER": "MAP — Панель управления",
    "SITE_SYMBOL": "hub",
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": False,
    # Цветовая схема — sky-blue, хорошо читается в светлой и тёмной теме
    "COLORS": {
        "primary": {
            "50": "240 249 255",
            "100": "224 242 254",
            "200": "186 230 253",
            "300": "125 211 252",
            "400": "56 189 248",
            "500": "14 165 233",
            "600": "2 132 199",
            "700": "3 105 161",
            "800": "7 89 133",
            "900": "12 74 110",
            "950": "8 47 73",
        },
    },
    "SIDEBAR": {
        "navigation": [
            {
                "title": "Аналитика",
                "separator": False,
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
                    {
                        "title": "Домены каталога",
                        "icon": "category",
                        "link": "/admin/tenants/catalogdomain/",
                    },
                    {"title": "Пользователи", "icon": "person", "link": "/admin/users/user/"},
                    {"title": "API-ключи", "icon": "key", "link": "/admin/tenants/apikey/"},
                    {
                        "title": "Вебхук-эндпоинты",
                        "icon": "webhook",
                        "link": "/admin/tenants/webhookendpoint/",
                    },
                ],
            },
            {
                "title": "Биллинг",
                "collapsible": True,
                "items": [
                    {"title": "Тарифные планы", "icon": "sell", "link": "/admin/billing/plan/"},
                    {
                        "title": "Подписки",
                        "icon": "subscriptions",
                        "link": "/admin/billing/subscription/",
                    },
                    {"title": "Счета", "icon": "receipt", "link": "/admin/billing/invoice/"},
                ],
            },
            {
                "title": "Товары и маркетплейсы",
                "collapsible": True,
                "items": [
                    {"title": "Товары", "icon": "inventory_2", "link": "/admin/products/product/"},
                    {
                        "title": "Листинги",
                        "icon": "storefront",
                        "link": "/admin/marketplaces/listing/",
                    },
                    {
                        "title": "Avito-аккаунты",
                        "icon": "manage_accounts",
                        "link": "/admin/marketplaces/marketplaceaccount/",
                    },
                    {
                        "title": "Источники данных",
                        "icon": "database",
                        "link": "/admin/datasources/datasourceconnection/",
                    },
                ],
            },
            {
                "title": "Изображения",
                "collapsible": True,
                "items": [
                    {
                        "title": "Логи поиска",
                        "icon": "image_search",
                        "link": "/admin/image_search/imagesearchlog/",
                    },
                    {
                        "title": "Кеш поиска",
                        "icon": "cached",
                        "link": "/admin/image_search/imagesearchcache/",
                    },
                ],
            },
            {
                "title": "Система",
                "collapsible": True,
                "items": [
                    {
                        "title": "Логи синхронизации",
                        "icon": "sync",
                        "link": "/admin/sync/synclog/",
                    },
                    {
                        "title": "Уведомления",
                        "icon": "notifications",
                        "link": "/admin/notifications/tenantnotificationsettings/",
                    },
                    {
                        "title": "Периодические задачи",
                        "icon": "schedule",
                        "link": "/admin/django_celery_beat/periodictask/",
                    },
                ],
            },
        ],
    },
}

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'corsheaders.middleware.CorsMiddleware',
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
        'schedule': 60 * 60 * 24,
        'options': {'queue': 'notifications'},
    },
    'billing-check-expired-daily': {
        'task': 'apps.billing.tasks.billing_check_expired',
        'schedule': 60 * 60 * 24,
        'options': {'queue': 'billing'},
    },
    'reset-monthly-ai-credits-daily': {
        'task': 'apps.billing.tasks.reset_monthly_ai_credits',
        'schedule': 60 * 60 * 24,
        'options': {'queue': 'billing'},
    },
    'update-tenant-counters-15min': {
        'task': 'apps.tenants.tasks.update_tenant_counters',
        'schedule': 60 * 15,
        'options': {'queue': 'sync_import'},
    },
    # Самосверка статусов с Avito: застрявшие pending → опрос, queued → публикация,
    # active → сверка модерации. Без неё статус в БД держался только на разовом
    # опросе при публикации и отставал от реального состояния на Avito.
    'check-moderation-status-10min': {
        'task': 'apps.marketplaces.tasks.check_moderation_status',
        'schedule': 60 * 10,
        'options': {'queue': 'avito_update'},
    },
}
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 3600
CELERY_TASK_SOFT_TIME_LIMIT = 3300

# Очереди и их приоритеты
CELERY_TASK_QUEUES = {
    'sync_import':        {'exchange': 'sync_import',        'routing_key': 'sync_import'},
    'avito_publish':      {'exchange': 'avito_publish',      'routing_key': 'avito_publish'},
    'avito_update':       {'exchange': 'avito_update',       'routing_key': 'avito_update'},
    'avito_price':        {'exchange': 'avito_price',        'routing_key': 'avito_price'},
    'avito_delete':       {'exchange': 'avito_delete',       'routing_key': 'avito_delete'},
    'ai_generate':        {'exchange': 'ai_generate',        'routing_key': 'ai_generate'},
    'notifications':      {'exchange': 'notifications',      'routing_key': 'notifications'},
    'billing':            {'exchange': 'billing',            'routing_key': 'billing'},
    'image_search':       {'exchange': 'image_search',       'routing_key': 'image_search'},
    'image_search_bulk':  {'exchange': 'image_search_bulk',  'routing_key': 'image_search_bulk'},
    'part_parsing':       {'exchange': 'part_parsing',       'routing_key': 'part_parsing'},
    'part_parsing_bulk':  {'exchange': 'part_parsing_bulk',  'routing_key': 'part_parsing_bulk'},
}
CELERY_TASK_DEFAULT_QUEUE = 'sync_import'

# --- DRF ---
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'apps.tenants.authentication.APIKeyAuthentication',
        'rest_framework_simplejwt.authentication.JWTAuthentication',
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

# --- JWT ---
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=30),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
    'TOKEN_OBTAIN_SERIALIZER': 'apps.tenants.jwt_serializers.TenantTokenObtainPairSerializer',
}

# --- CORS ---
CORS_ALLOWED_ORIGINS = os.environ.get('CORS_ALLOWED_ORIGINS', 'http://localhost:3000').split(',')
CORS_ALLOW_CREDENTIALS = True

# --- Swagger ---
SPECTACULAR_SETTINGS = {
    'TITLE': 'MAP API',
    'DESCRIPTION': (
        'Marketplace Automation Platform — API для автоматизации товарных объявлений на Avito.\n\n'
        '## Аутентификация\n\n'
        'Поддерживаются два метода:\n\n'
        '**API Key** (рекомендуется для интеграций):\n'
        '```\nAuthorization: Bearer map_sk_<ваш_ключ>\n```\n\n'
        '**JWT Token** (для веб-приложений):\n'
        '```\nAuthorization: Bearer <access_token>\n```\n\n'
        'Получить JWT: `POST /api/v1/auth/token/`  \n'
        'Создать API Key: `POST /api/v1/tenant/api-keys/`'
    ),
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'SECURITY': [{'Bearer': []}],
    'COMPONENTS': {
        'securitySchemes': {
            'Bearer': {
                'type': 'http',
                'scheme': 'bearer',
                'bearerFormat': 'API Key or JWT',
                'description': 'API Key (map_sk_...) или JWT access token',
            },
        },
    },
    'TAGS': [
        {'name': 'Auth', 'description': 'Регистрация, JWT, информация о текущем пользователе'},
        {'name': 'Tenant', 'description': 'Организация и её пользователи'},
        {'name': 'API Keys', 'description': 'Управление API-ключами'},
        {'name': 'Webhooks', 'description': 'Вебхук-эндпоинты для получения событий'},
        {'name': 'Products', 'description': 'Каталог товаров'},
        {'name': 'Listings', 'description': 'Объявления на маркетплейсах'},
        {'name': 'Accounts', 'description': 'Аккаунты маркетплейсов (Avito)'},
        {'name': 'Analytics', 'description': 'Статистика просмотров и CTR'},
        {'name': 'Billing', 'description': 'Тарифы, подписки, платежи'},
    ],
    'SORT_OPERATIONS': False,
}

# --- Файловое хранилище (Yandex Cloud S3) ---
YC_S3_BUCKET = os.environ.get('YC_S3_BUCKET', '')
YC_S3_ACCESS_KEY = os.environ.get('YC_S3_ACCESS_KEY', '')
YC_S3_SECRET_KEY = os.environ.get('YC_S3_SECRET_KEY', '')
YC_CDN_DOMAIN = os.environ.get('YC_CDN_DOMAIN', '')
MEDIA_KEY_PREFIX = os.environ.get('MEDIA_KEY_PREFIX', '').strip('/')

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
                'default_acl': 'public-read',
                # public-read: изображения товаров не являются приватными данными;
                # presigned-URLs с custom_domain не работают — используем прямые публичные URL
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

# --- Image Search ---
BRAVE_SEARCH_API_KEY = os.environ.get('BRAVE_SEARCH_API_KEY', '')

# --- Avito ---
AVITO_CLIENT_ID = os.environ.get('AVITO_CLIENT_ID', '')
AVITO_CLIENT_SECRET = os.environ.get('AVITO_CLIENT_SECRET', '')

# --- Биллинг ---
YOOKASSA_SHOP_ID = os.environ.get('YOOKASSA_SHOP_ID', '')
YOOKASSA_SECRET_KEY = os.environ.get('YOOKASSA_SECRET_KEY', '')
SITE_URL = os.environ.get('SITE_URL', 'http://localhost:8000')
FRONTEND_URL = os.environ.get('FRONTEND_URL', 'http://localhost:3000')

# --- Уведомления ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_BOT_USERNAME = os.environ.get('TELEGRAM_BOT_USERNAME', '')  # без @, напр. MyMapBot
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
LANGUAGE_CODE = 'ru'
LANGUAGES = [('ru', 'Russian')]
LOCALE_PATHS = [BASE_DIR / 'locale']
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

# --- Поиск изображений ---
from config.settings.image_search import *  # noqa: E402, F403
