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
    'rest_framework_simplejwt.token_blacklist',
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
    'apps.media_processing',
    'apps.web_research',
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
                "title": "AI и генерация",
                "collapsible": True,
                "items": [
                    {
                        "title": "Шаблоны промптов",
                        "icon": "description",
                        "link": "/admin/ai_agent/aiprompttemplate/",
                    },
                    {
                        "title": "AI-модели",
                        "icon": "smart_toy",
                        "link": "/admin/ai_agent/aimodel/",
                    },
                    {
                        "title": "Настройки AI тенантов",
                        "icon": "tune",
                        "link": "/admin/ai_agent/tenantaisettings/",
                    },
                    {
                        "title": "AI-запросы",
                        "icon": "history",
                        "link": "/admin/ai_agent/airequestlog/",
                    },
                    {
                        "title": "Интернет-исследования",
                        "icon": "travel_explore",
                        "link": "/admin/web_research/webresearchrun/",
                    },
                    {
                        "title": "Сервисы интернет-поиска",
                        "icon": "language",
                        "link": "/admin/web_research/websearchconnection/",
                    },
                    {
                        "title": "Цены AI-провайдеров",
                        "icon": "price_change",
                        "link": "/admin/ai_agent/aiproviderprice/",
                    },
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
                    {
                        "title": "Проверки изображений",
                        "icon": "fact_check",
                        "link": "/admin/media_processing/imageassessment/",
                    },
                    {
                        "title": "Задачи обработки медиа",
                        "icon": "auto_fix_high",
                        "link": "/admin/media_processing/mediaprocessingjob/",
                    },
                    {
                        "title": "Варианты изображений",
                        "icon": "collections",
                        "link": "/admin/media_processing/productimagevariant/",
                    },
                    {
                        "title": "Пресеты обработки",
                        "icon": "tune",
                        "link": "/admin/media_processing/mediaprocessingpreset/",
                    },
                    {
                        "title": "Медиа-провайдеры",
                        "icon": "hub",
                        "link": "/admin/media_processing/mediaproviderpolicy/",
                    },
                    {
                        "title": "Настройки медиа тенантов",
                        "icon": "settings_suggest",
                        "link": "/admin/media_processing/tenantmediasettings/",
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
# REDIS_URL остаётся только development fallback. Production обязан задавать
# отдельные URL для eviction-cache и durable Celery/coordination Redis.
REDIS_URL = os.environ.get('REDIS_URL', '').strip() or 'redis://redis:6379/0'
CACHE_REDIS_URL = os.environ.get('CACHE_REDIS_URL', '').strip() or REDIS_URL
CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', '').strip() or REDIS_URL
CELERY_RESULT_BACKEND = (
    os.environ.get('CELERY_RESULT_BACKEND', '').strip() or CELERY_BROKER_URL
)
COORDINATION_REDIS_URL = (
    os.environ.get('COORDINATION_REDIS_URL', '').strip() or CELERY_BROKER_URL
)

CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': CACHE_REDIS_URL,
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
        },
        'KEY_PREFIX': 'map',
    },
    'coordination': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': COORDINATION_REDIS_URL,
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
        },
        'KEY_PREFIX': 'map_coord',
    },
}

# --- Celery ---
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'Europe/Moscow'
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'
# Единственный источник расписания — management command setup_periodic_tasks.
# Смешивание beat_schedule и DatabaseScheduler создавало дубли одних и тех же задач.
CELERY_BEAT_SCHEDULE = {}
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 3600
CELERY_TASK_SOFT_TIME_LIMIT = 3300
CELERY_TASK_IGNORE_RESULT = True
CELERY_RESULT_EXPIRES = 86400
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TASK_PUBLISH_RETRY = True
CELERY_TASK_PUBLISH_RETRY_POLICY = {
    'max_retries': 5,
    'interval_start': 0,
    'interval_step': 0.5,
    'interval_max': 3,
}
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_TRANSPORT_OPTIONS = {
    'visibility_timeout': 14400,
    'global_keyprefix': 'map_broker_',
}
CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS = {
    'global_keyprefix': 'map_result_',
}

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
    'media_processing':   {'exchange': 'media_processing',   'routing_key': 'media_processing'},
    'part_parsing':       {'exchange': 'part_parsing',       'routing_key': 'part_parsing'},
    'part_parsing_bulk':  {'exchange': 'part_parsing_bulk',  'routing_key': 'part_parsing_bulk'},
}
CELERY_TASK_DEFAULT_QUEUE = 'sync_import'
CELERY_TASK_ROUTES = {
    'apps.image_search.tasks.search_images_for_product': {'queue': 'image_search'},
}

# --- DRF ---
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'apps.tenants.authentication.APIKeyAuthentication',
        'apps.tenants.authentication.TenantJWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
        'apps.tenants.permissions.TenantRolePermission',
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
    'ENUM_NAME_OVERRIDES': {
        'ListingStatusEnum': 'apps.marketplaces.models.Listing.STATUS_CHOICES',
        'WebResearchRunStatusEnum': (
            'apps.web_research.models.WEB_RESEARCH_RUN_STATUS_CHOICES'
        ),
        'ProductConditionEnum': 'apps.products.models.Product.CONDITION_CHOICES',
        'ProductReviewStatusEnum': 'apps.products.models.ReviewStatus',
        'CompetitorOfferConditionEnum': (
            'apps.web_research.models.COMPETITOR_OFFER_CONDITION_CHOICES'
        ),
        'CompetitorOfferReviewStatusEnum': (
            'apps.web_research.models.COMPETITOR_OFFER_REVIEW_STATUS_CHOICES'
        ),
        'DataSourceTypeEnum': 'apps.datasources.models.DataSourceConnection.TYPE_CHOICES',
    },
    'TAGS': [
        {'name': 'Auth', 'description': 'Регистрация, JWT, информация о текущем пользователе'},
        {'name': 'Tenant', 'description': 'Организация и её пользователи'},
        {'name': 'Profile', 'description': 'Профиль и настройки текущего пользователя'},
        {'name': 'API Keys', 'description': 'Управление API-ключами'},
        {'name': 'Webhooks', 'description': 'Вебхук-эндпоинты для получения событий'},
        {'name': 'Products', 'description': 'Каталог товаров'},
        {'name': 'Catalog Categories', 'description': 'Категории внутреннего каталога'},
        {'name': 'Data sources', 'description': 'Подключения 1С, XML и CSV-импорт'},
        {'name': 'Listings', 'description': 'Объявления на маркетплейсах'},
        {'name': 'Accounts', 'description': 'Аккаунты маркетплейсов (Avito)'},
        {'name': 'Category mappings', 'description': 'Сопоставление категорий с Avito'},
        {'name': 'Analytics', 'description': 'Статистика просмотров и CTR'},
        {'name': 'Billing', 'description': 'Тарифы, подписки, платежи'},
        {'name': 'AI', 'description': 'AI-модели, настройки и расход кредитов'},
        {'name': 'Images', 'description': 'Поиск и отбор изображений товаров'},
        {'name': 'Media processing', 'description': 'Обработка и варианты изображений'},
        {'name': 'Web research', 'description': 'Товарные и рыночные интернет-исследования'},
        {'name': 'Notifications', 'description': 'Telegram, email и настройки уведомлений'},
        {'name': 'Logs', 'description': 'Журнал синхронизаций и операций'},
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
                # presigned-URLs с custom_domain не работают — используем прямые публичные URL.
                # querystring_auth=False: .url() отдаёт постоянную ссылку без подписи и
                # срока действия (иначе Avito не успеет скачать фото из фида за 1 час).
                'querystring_auth': False,
                'custom_domain': YC_CDN_DOMAIN or None,
            },
        },
        'staticfiles': {
            'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
        },
    }

# --- Безопасность ---
FIELD_ENCRYPTION_KEY = os.environ.get('FIELD_ENCRYPTION_KEY', '')
FIELD_ENCRYPTION_KEYS = [
    key.strip()
    for key in os.environ.get('FIELD_ENCRYPTION_KEYS', FIELD_ENCRYPTION_KEY).split(',')
    if key.strip()
]
WEBHOOK_REQUEST_TIMEOUT_SECONDS = int(os.environ.get('WEBHOOK_REQUEST_TIMEOUT_SECONDS', '10'))
WEBHOOK_MAX_ATTEMPTS = int(os.environ.get('WEBHOOK_MAX_ATTEMPTS', '8'))

# --- Retention ---
SOFT_DELETE_RETENTION_DAYS = int(os.environ.get('SOFT_DELETE_RETENTION_DAYS', '90'))
WEBHOOK_AUDIT_RETENTION_DAYS = int(os.environ.get('WEBHOOK_AUDIT_RETENTION_DAYS', '180'))
BILLING_AUDIT_RETENTION_DAYS = int(os.environ.get('BILLING_AUDIT_RETENTION_DAYS', '730'))
SYNC_LOG_RETENTION_DAYS = int(os.environ.get('SYNC_LOG_RETENTION_DAYS', '90'))

# --- AI ---
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
MOONSHOT_API_KEY = os.environ.get('MOONSHOT_API_KEY', '')

# --- Image Search ---
BRAVE_SEARCH_API_KEY = os.environ.get('BRAVE_SEARCH_API_KEY', '')
TAVILY_API_KEY = os.environ.get('TAVILY_API_KEY', '')
WEB_RESEARCH_AUTO_FALLBACK = os.environ.get(
    'WEB_RESEARCH_AUTO_FALLBACK', 'true',
).lower() in {'1', 'true', 'yes'}
WEB_RESEARCH_COVERAGE_THRESHOLD = float(os.environ.get(
    'WEB_RESEARCH_COVERAGE_THRESHOLD', '0.65',
))
WEB_RESEARCH_MAX_QUERIES = int(os.environ.get('WEB_RESEARCH_MAX_QUERIES', '2'))
WEB_RESEARCH_RESULTS_PER_QUERY = int(os.environ.get('WEB_RESEARCH_RESULTS_PER_QUERY', '8'))

# --- Avito ---
AVITO_CLIENT_ID = os.environ.get('AVITO_CLIENT_ID', '')
AVITO_CLIENT_SECRET = os.environ.get('AVITO_CLIENT_SECRET', '')

# --- Биллинг ---
YOOKASSA_SHOP_ID = os.environ.get('YOOKASSA_SHOP_ID', '')
YOOKASSA_SECRET_KEY = os.environ.get('YOOKASSA_SECRET_KEY', '')
BILLING_TRIAL_DAYS = int(os.environ.get('BILLING_TRIAL_DAYS', '14'))
BILLING_GRACE_PERIOD_DAYS = int(os.environ.get('BILLING_GRACE_PERIOD_DAYS', '7'))
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
