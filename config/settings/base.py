import os
from datetime import timedelta
from pathlib import Path

import dj_database_url

from apps.marketplaces.feed_endpoint import (
    FeedEndpointConfigurationError,
    PUBLIC_FEED_PATH,
    canonical_marketplace_feed_cdn_origin,
    canonical_marketplace_feed_public_base_url,
    parse_marketplace_feed_url_signing_keys,
)


_SAFE_FEED_ARTIFACT_BUCKET_CHARACTERS = frozenset(
    'abcdefghijklmnopqrstuvwxyz0123456789.-',
)


def _is_safe_feed_artifact_bucket(value: str) -> bool:
    """Validate one lowercase DNS-style bucket name without network I/O."""

    if (
        not 3 <= len(value) <= 63
        or value[0] not in 'abcdefghijklmnopqrstuvwxyz0123456789'
        or value[-1] not in 'abcdefghijklmnopqrstuvwxyz0123456789'
        or any(
            character not in _SAFE_FEED_ARTIFACT_BUCKET_CHARACTERS
            for character in value
        )
        or '..' in value
        or '.-' in value
        or '-.' in value
    ):
        return False
    labels = value.split('.')
    return not (
        len(labels) == 4
        and all(label.isascii() and label.isdecimal() for label in labels)
    )


def _strict_feed_artifact_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = os.environ.get(name, str(default)).strip()
    if not raw_value.isascii() or not raw_value.isdecimal():
        raise ValueError(f'{name} must be an ASCII integer.')
    value = int(raw_value)
    if not minimum <= value <= maximum:
        raise ValueError(f'{name} must be between {minimum} and {maximum}.')
    return value


def _strict_feed_cutover_account_ids(raw_value: str) -> tuple[int, ...]:
    """Parse one canonical, bounded CSV allowlist of positive account IDs."""

    if raw_value == '':
        return ()
    values = raw_value.split(',')
    if len(values) > 10:
        raise ValueError(
            'MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS may contain at most 10 IDs.',
        )
    if any(
        not value
        or not value.isascii()
        or not value.isdecimal()
        or value.startswith('0')
        for value in values
    ):
        raise ValueError(
            'MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS must be canonical positive '
            'ASCII integers separated by commas.',
        )
    account_ids = tuple(int(value) for value in values)
    if len(set(account_ids)) != len(account_ids) or account_ids != tuple(
        sorted(account_ids)
    ):
        raise ValueError(
            'MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS must be unique and sorted.',
        )
    return account_ids


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
    'apps.core.apps.CoreConfig',
    'apps.users',
    'apps.tenants',
    'apps.billing',
    'apps.datasources',
    'apps.products',
    'apps.marketplaces',
    'apps.ai_agent.apps.AiAgentConfig',
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
                    {
                        "title": "Billing outbox",
                        "icon": "outbox",
                        "link": "/admin/billing/billingoutboxevent/",
                    },
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
            # Coordination is on business hot paths and in staff telemetry.
            # A failed durable Redis must fail fast instead of hanging workers.
            'SOCKET_CONNECT_TIMEOUT': 2,
            'SOCKET_TIMEOUT': 2,
        },
        'KEY_PREFIX': 'map_coord',
    },
}

# --- Celery ---
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_PROTOCOL = 2
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'Europe/Moscow'
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'
# Единственный источник расписания — management command setup_periodic_tasks.
# Смешивание beat_schedule и DatabaseScheduler создавало дубли одних и тех же задач.
CELERY_BEAT_SCHEDULE: dict[str, dict] = {}
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
# Paid AI calls use a durable pre-call accounting record. Calls have a 120s
# HTTP timeout, so ten minutes is a conservative hard-crash recovery window;
# reservations that never crossed the network boundary can be released sooner.
AI_PROVIDER_STARTED_STALE_SECONDS = max(
    300,
    int(os.environ.get('AI_PROVIDER_STARTED_STALE_SECONDS', '600')),
)
AI_PROVIDER_NEVER_STARTED_STALE_SECONDS = max(
    60,
    int(os.environ.get('AI_PROVIDER_NEVER_STARTED_STALE_SECONDS', '300')),
)
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
CELERY_TASK_CREATE_MISSING_QUEUES = False
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
    'DEFAULT_THROTTLE_RATES': {
        'expensive_image_principal': '6/min',
        'expensive_image_tenant': '12/min',
        'expensive_research_principal': '3/min',
        'expensive_research_tenant': '6/min',
        'image_upload_principal': '12/min',
        'image_upload_tenant': '30/min',
        'webhook_create_principal': '10/hour',
        'webhook_create_tenant': '20/hour',
        'webhook_test_principal': '6/min',
        'webhook_test_tenant': '20/min',
        'datasource_test_principal': '5/min',
        'datasource_test_tenant': '10/min',
        'datasource_sync_principal': '2/min',
        'datasource_sync_tenant': '4/min',
        'datasource_upload_principal': '2/hour',
        'datasource_upload_tenant': '6/hour',
        'auth_login': '20/min',
        'auth_register': '10/hour',
        'auth_refresh': '60/min',
        'billing_checkout': '6/min',
        'password_reset_request': '5/hour',
        'password_reset_confirm': '10/hour',
    },
    # Production traffic проходит через один nginx hop. Берём последний адрес
    # из X-Forwarded-For, который nginx добавляет сам, а не доверяем всей цепочке.
    'NUM_PROXIES': int(os.environ.get('API_NUM_PROXIES', '1')),
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
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get('CSRF_TRUSTED_ORIGINS', 'http://localhost:3000').split(',')
    if origin.strip()
]
CSRF_FAILURE_VIEW = 'apps.tenants.csrf.csrf_failure'

# Refresh token браузерной сессии никогда не доступен JavaScript-коду.
AUTH_REFRESH_COOKIE_NAME = os.environ.get('AUTH_REFRESH_COOKIE_NAME', 'map_refresh')
AUTH_REFRESH_COOKIE_PATH = '/api/v1/auth/browser/'
AUTH_REFRESH_COOKIE_SAMESITE = 'Lax'
PASSWORD_RESET_TIMEOUT = min(
    24 * 60 * 60,
    max(5 * 60, int(os.environ.get('PASSWORD_RESET_TIMEOUT', '3600'))),
)

# Bound non-file request bodies at Django as a second line behind nginx.
# Uploaded files are streamed to disk once they exceed the much smaller memory cap.
DATA_UPLOAD_MAX_MEMORY_SIZE = min(
    16 * 1024 * 1024,
    max(
        1024,
        int(os.environ.get('API_REQUEST_MAX_BYTES', str(12 * 1024 * 1024))),
    ),
)
FILE_UPLOAD_MAX_MEMORY_SIZE = min(
    2 * 1024 * 1024,
    max(
        1024,
        int(os.environ.get('FILE_UPLOAD_MEMORY_MAX_BYTES', str(1024 * 1024))),
    ),
)
DATA_UPLOAD_MAX_NUMBER_FIELDS = min(
    5000,
    max(1, int(os.environ.get('DATA_UPLOAD_MAX_NUMBER_FIELDS', '2000'))),
)

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
        'Создать API Key: `POST /api/v1/tenant/api-keys/`\n\n'
        '### Browser session flow\n\n'
        '1. `GET /api/v1/auth/browser/csrf/`, затем передавайте значение '
        '`csrf_token` в `X-CSRFToken` для каждого browser POST.\n'
        '2. `POST /api/v1/auth/browser/login/`: access token возвращается в JSON, '
        'refresh хранится только в HttpOnly cookie.\n'
        '3. Передавайте access token как Bearer; при его истечении вызывайте '
        '`POST /api/v1/auth/browser/refresh/` с cookie и CSRF header.\n'
        '4. `401 unauthorized` означает завершённую сессию и требует входа; '
        '`403 csrf_failed` означает неверную CSRF cookie/header pair.\n'
        'Refresh cookie нельзя читать или копировать в JavaScript storage.'
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
        'APIKeyRoleEnum': 'apps.tenants.models.APIKey.ROLE_CHOICES',
        'TenantUserRoleEnum': 'apps.tenants.models.TenantUser.ROLES',
    },
    'TAGS': [
        {'name': 'Auth', 'description': 'Регистрация, JWT, информация о текущем пользователе'},
        {
            'name': 'Browser Auth',
            'description': (
                'CSRF-защищённая браузерная сессия с HttpOnly refresh cookie. '
                'Сначала получите CSRF token, затем передавайте X-CSRFToken во все POST.'
            ),
        },
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
        {'name': 'Dashboard', 'description': 'Сводка состояния и ключевых показателей тенанта'},
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
YC_S3_BUCKET = os.environ.get('YC_S3_BUCKET', '').strip()
YC_S3_ACCESS_KEY = os.environ.get('YC_S3_ACCESS_KEY', '').strip()
YC_S3_SECRET_KEY = os.environ.get('YC_S3_SECRET_KEY', '').strip()
YC_CDN_DOMAIN = os.environ.get('YC_CDN_DOMAIN', '').strip()
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
WEBHOOK_REQUEST_TIMEOUT_SECONDS = min(
    30,
    max(1, int(os.environ.get('WEBHOOK_REQUEST_TIMEOUT_SECONDS', '10'))),
)
WEBHOOK_MAX_ATTEMPTS = min(
    20,
    max(1, int(os.environ.get('WEBHOOK_MAX_ATTEMPTS', '8'))),
)
WEBHOOK_ENDPOINTS_PER_TENANT = min(
    100,
    max(1, int(os.environ.get('WEBHOOK_ENDPOINTS_PER_TENANT', '20'))),
)
WEBHOOK_DISPATCH_BATCH_SIZE = min(
    500,
    max(1, int(os.environ.get('WEBHOOK_DISPATCH_BATCH_SIZE', '100'))),
)
MAX_IMAGE_UPLOAD_BYTES = min(
    5 * 1024 * 1024,
    max(1, int(os.environ.get('MAX_IMAGE_UPLOAD_BYTES', str(5 * 1024 * 1024)))),
)
MAX_DECODED_IMAGE_PIXELS = min(
    16_000_000,
    max(1, int(os.environ.get('MAX_DECODED_IMAGE_PIXELS', '16000000'))),
)
MEDIA_PROVIDER_OUTPUT_MAX_BYTES = min(
    25 * 1024 * 1024,
    max(
        1,
        int(os.environ.get('MEDIA_PROVIDER_OUTPUT_MAX_BYTES', str(25 * 1024 * 1024))),
    ),
)
API_BULK_MAX_ITEMS = min(
    500,
    max(1, int(os.environ.get('API_BULK_MAX_ITEMS', '500'))),
)
DATASOURCE_UPLOAD_MAX_BYTES = min(
    5 * 1024 * 1024,
    max(1, int(os.environ.get('DATASOURCE_UPLOAD_MAX_BYTES', str(5 * 1024 * 1024)))),
)
DATASOURCE_XLSX_MAX_UNCOMPRESSED_BYTES = min(
    25 * 1024 * 1024,
    max(
        1,
        int(os.environ.get(
            'DATASOURCE_XLSX_MAX_UNCOMPRESSED_BYTES',
            str(25 * 1024 * 1024),
        )),
    ),
)
DATASOURCE_XLSX_MAX_ARCHIVE_ENTRIES = min(
    1024,
    max(1, int(os.environ.get('DATASOURCE_XLSX_MAX_ARCHIVE_ENTRIES', '1024'))),
)
DATASOURCE_IMPORT_MAX_ROWS = min(
    5000,
    max(1, int(os.environ.get('DATASOURCE_IMPORT_MAX_ROWS', '5000'))),
)
DATASOURCE_IMPORT_MAX_COLUMNS = min(
    128,
    max(1, int(os.environ.get('DATASOURCE_IMPORT_MAX_COLUMNS', '128'))),
)
DATASOURCE_IMPORT_MAX_CELLS = min(
    100_000,
    max(1, int(os.environ.get('DATASOURCE_IMPORT_MAX_CELLS', '100000'))),
)
DATASOURCE_XML_MAX_BYTES = min(
    8 * 1024 * 1024,
    max(1, int(os.environ.get('DATASOURCE_XML_MAX_BYTES', str(8 * 1024 * 1024)))),
)
DATASOURCE_HTTP_MAX_BYTES = min(
    5 * 1024 * 1024,
    max(1, int(os.environ.get('DATASOURCE_HTTP_MAX_BYTES', str(5 * 1024 * 1024)))),
)
DATASOURCE_XML_MAX_NODES = min(
    60_000,
    max(1, int(os.environ.get('DATASOURCE_XML_MAX_NODES', '60000'))),
)
DATASOURCE_XML_MAX_TEXT_CHARS = min(
    4 * 1024 * 1024,
    max(
        1,
        int(os.environ.get('DATASOURCE_XML_MAX_TEXT_CHARS', str(4 * 1024 * 1024))),
    ),
)
DATASOURCE_XML_MAX_ITEMS = min(
    5000,
    max(1, int(os.environ.get('DATASOURCE_XML_MAX_ITEMS', '5000'))),
)
DATASOURCE_FETCH_PAGE_MAX_ITEMS = min(
    500,
    max(1, int(os.environ.get('DATASOURCE_FETCH_PAGE_MAX_ITEMS', '500'))),
)
PART_PAGE_MAX_BYTES = min(
    2 * 1024 * 1024,
    max(1, int(os.environ.get('PART_PAGE_MAX_BYTES', str(2 * 1024 * 1024)))),
)
AVITO_API_RESPONSE_MAX_BYTES = min(
    5 * 1024 * 1024,
    max(1, int(os.environ.get('AVITO_API_RESPONSE_MAX_BYTES', str(5 * 1024 * 1024)))),
)
AVITO_API_MAX_PAGES = min(
    100, max(1, int(os.environ.get('AVITO_API_MAX_PAGES', '100'))),
)
AVITO_TREE_MAX_DEPTH = min(
    32, max(1, int(os.environ.get('AVITO_TREE_MAX_DEPTH', '12'))),
)
AVITO_TREE_MAX_NODES = min(
    20000, max(1, int(os.environ.get('AVITO_TREE_MAX_NODES', '10000'))),
)
AVITO_TREE_MAX_LEAVES = min(
    10000, max(1, int(os.environ.get('AVITO_TREE_MAX_LEAVES', '2000'))),
)
AVITO_TREE_MAX_TOTAL_CALLS = min(
    10000, max(1, int(os.environ.get('AVITO_TREE_MAX_TOTAL_CALLS', '3000'))),
)
AVITO_STATUS_LIFECYCLE_MODE = os.environ.get(
    'AVITO_STATUS_LIFECYCLE_MODE', 'legacy',
).strip().lower()
if AVITO_STATUS_LIFECYCLE_MODE not in {'legacy', 'dual_write'}:
    raise ValueError(
        'AVITO_STATUS_LIFECYCLE_MODE должен быть legacy или dual_write.',
    )
MARKETPLACE_FEED_RUN_MODE = os.environ.get(
    'MARKETPLACE_FEED_RUN_MODE', 'legacy',
).strip().lower()
if MARKETPLACE_FEED_RUN_MODE not in {'legacy', 'durable'}:
    raise ValueError(
        'MARKETPLACE_FEED_RUN_MODE должен быть legacy или durable.',
    )
MARKETPLACE_FEED_INGRESS_MODE = os.environ.get(
    'MARKETPLACE_FEED_INGRESS_MODE', 'legacy',
).strip().lower()
if MARKETPLACE_FEED_INGRESS_MODE not in {'legacy', 'dual_write'}:
    raise ValueError(
        'MARKETPLACE_FEED_INGRESS_MODE must be legacy or dual_write.',
    )
MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS = _strict_feed_cutover_account_ids(
    os.environ.get('MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS', '').strip(),
)
MARKETPLACE_FEED_ARTIFACT_MODE = os.environ.get(
    'MARKETPLACE_FEED_ARTIFACT_MODE', 'disabled',
).strip().lower()
if MARKETPLACE_FEED_ARTIFACT_MODE not in {
    'disabled', 'shadow', 'canary', 'active',
}:
    raise ValueError(
        'MARKETPLACE_FEED_ARTIFACT_MODE must be disabled, shadow, canary, '
        'or active.',
    )
MARKETPLACE_FEED_ARTIFACT_BUCKET = os.environ.get(
    'MARKETPLACE_FEED_ARTIFACT_BUCKET', '',
).strip()
if (
    MARKETPLACE_FEED_ARTIFACT_BUCKET
    and not _is_safe_feed_artifact_bucket(MARKETPLACE_FEED_ARTIFACT_BUCKET)
):
    raise ValueError(
        'MARKETPLACE_FEED_ARTIFACT_BUCKET must be a safe lowercase S3 bucket.',
    )
MARKETPLACE_FEED_ARTIFACT_ACCESS_KEY_ID = os.environ.get(
    'MARKETPLACE_FEED_ARTIFACT_ACCESS_KEY_ID', '',
).strip()
MARKETPLACE_FEED_ARTIFACT_SECRET_ACCESS_KEY = os.environ.get(
    'MARKETPLACE_FEED_ARTIFACT_SECRET_ACCESS_KEY', '',
).strip()
MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER = os.environ.get(
    'MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER', '',
).strip()
MARKETPLACE_FEED_ARTIFACT_KMS_KEY_ID = os.environ.get(
    'MARKETPLACE_FEED_ARTIFACT_KMS_KEY_ID', '',
).strip()
if MARKETPLACE_FEED_ARTIFACT_MODE != 'disabled' and not all((
    MARKETPLACE_FEED_ARTIFACT_BUCKET,
    MARKETPLACE_FEED_ARTIFACT_ACCESS_KEY_ID,
    MARKETPLACE_FEED_ARTIFACT_SECRET_ACCESS_KEY,
    MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER,
    MARKETPLACE_FEED_ARTIFACT_KMS_KEY_ID,
)):
    raise ValueError(
        'MARKETPLACE_FEED_ARTIFACT_BUCKET, '
        'MARKETPLACE_FEED_ARTIFACT_ACCESS_KEY_ID, '
        'MARKETPLACE_FEED_ARTIFACT_SECRET_ACCESS_KEY, '
        'MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER and '
        'MARKETPLACE_FEED_ARTIFACT_KMS_KEY_ID are required unless artifact '
        'mode is disabled.',
    )
MARKETPLACE_FEED_ARTIFACT_MAX_BYTES = _strict_feed_artifact_int(
    'MARKETPLACE_FEED_ARTIFACT_MAX_BYTES',
    268_435_456,
    minimum=1,
    maximum=1_073_741_824,
)
MARKETPLACE_FEED_REDIRECT_TTL_SECONDS = _strict_feed_artifact_int(
    'MARKETPLACE_FEED_REDIRECT_TTL_SECONDS',
    120,
    minimum=30,
    maximum=300,
)
_MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED_RAW = os.environ.get(
    'MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED', 'false',
).strip().lower()
if _MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED_RAW not in {'true', 'false'}:
    raise ValueError(
        'MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED must be true or false.',
    )
MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED = (
    _MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED_RAW == 'true'
)
_MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS_RAW = os.environ.get(
    'MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS', '3600',
).strip()
if (
    not _MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS_RAW.isascii()
    or not _MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS_RAW.isdecimal()
):
    raise ValueError(
        'MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS must be an integer.',
    )
MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS = int(
    _MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS_RAW,
)
if not 300 <= MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS <= 86_400:
    raise ValueError(
        'MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS must be between '
        '300 and 86400.',
    )
TRUSTED_API_RESPONSE_MAX_BYTES = min(
    5 * 1024 * 1024,
    max(1, int(os.environ.get('TRUSTED_API_RESPONSE_MAX_BYTES', str(5 * 1024 * 1024)))),
)

# --- Retention ---
SOFT_DELETE_RETENTION_DAYS = min(
    3650, max(1, int(os.environ.get('SOFT_DELETE_RETENTION_DAYS', '90'))),
)
WEBHOOK_AUDIT_RETENTION_DAYS = min(
    3650, max(1, int(os.environ.get('WEBHOOK_AUDIT_RETENTION_DAYS', '180'))),
)
BILLING_AUDIT_RETENTION_DAYS = min(
    3650, max(1, int(os.environ.get('BILLING_AUDIT_RETENTION_DAYS', '730'))),
)
SYNC_LOG_RETENTION_DAYS = min(
    3650, max(1, int(os.environ.get('SYNC_LOG_RETENTION_DAYS', '90'))),
)
NOTIFICATION_DELIVERY_RETENTION_DAYS = min(
    3650,
    max(30, int(os.environ.get('NOTIFICATION_DELIVERY_RETENTION_DAYS', '180'))),
)
NOTIFICATION_DELIVERY_CLAIM_TIMEOUT_SECONDS = min(
    3600,
    max(30, int(os.environ.get('NOTIFICATION_DELIVERY_CLAIM_TIMEOUT_SECONDS', '120'))),
)
PRODUCT_PARSE_RAW_HTML_RETENTION_DAYS = min(
    365, max(1, int(os.environ.get('PRODUCT_PARSE_RAW_HTML_RETENTION_DAYS', '14'))),
)
PRODUCT_PARSE_JOB_RETENTION_DAYS = min(
    3650, max(1, int(os.environ.get('PRODUCT_PARSE_JOB_RETENTION_DAYS', '180'))),
)
IMAGE_SEARCH_LOG_RETENTION_DAYS = min(
    3650, max(1, int(os.environ.get('IMAGE_SEARCH_LOG_RETENTION_DAYS', '90'))),
)
IMAGE_SEARCH_TASK_RETENTION_DAYS = min(
    3650, max(1, int(os.environ.get('IMAGE_SEARCH_TASK_RETENTION_DAYS', '30'))),
)
PRODUCT_BULK_ACTION_JOB_RETENTION_DAYS = min(
    3650, max(1, int(os.environ.get('PRODUCT_BULK_ACTION_JOB_RETENTION_DAYS', '90'))),
)
MEDIA_PROCESSING_JOB_RETENTION_DAYS = min(
    3650, max(1, int(os.environ.get('MEDIA_PROCESSING_JOB_RETENTION_DAYS', '180'))),
)
BACKGROUND_JOB_RETENTION_DAYS = min(
    3650, max(1, int(os.environ.get('BACKGROUND_JOB_RETENTION_DAYS', '30'))),
)
AI_PROVIDER_OPERATION_RETENTION_DAYS = min(
    3650,
    max(30, int(os.environ.get('AI_PROVIDER_OPERATION_RETENTION_DAYS', '730'))),
)
WEB_SEARCH_ATTEMPT_RETENTION_DAYS = min(
    3650,
    max(30, int(os.environ.get('WEB_SEARCH_ATTEMPT_RETENTION_DAYS', '730'))),
)
WEB_SEARCH_STARTED_STALE_SECONDS = min(
    86400,
    # Must exceed the longest durable worker lease. Resolving a younger row
    # could release its domain fence while the original HTTP call is alive.
    max(3700, int(os.environ.get('WEB_SEARCH_STARTED_STALE_SECONDS', '7200'))),
)
WEB_SEARCH_CHECKPOINT_MAX_BYTES = min(
    4 * 1024 * 1024,
    max(64 * 1024, int(os.environ.get('WEB_SEARCH_CHECKPOINT_MAX_BYTES', str(1024 * 1024)))),
)
WEB_SEARCH_WORKFLOW_INPUT_MAX_BYTES = min(
    512 * 1024,
    max(8 * 1024, int(os.environ.get('WEB_SEARCH_WORKFLOW_INPUT_MAX_BYTES', str(128 * 1024)))),
)
RETENTION_PURGE_BATCH_SIZE = min(
    10000, max(1, int(os.environ.get('RETENTION_PURGE_BATCH_SIZE', '1000'))),
)

# --- AI ---
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
MOONSHOT_API_KEY = os.environ.get('MOONSHOT_API_KEY', '')

# --- Image Search ---
BRAVE_SEARCH_API_KEY = os.environ.get('BRAVE_SEARCH_API_KEY', '')
TAVILY_API_KEY = os.environ.get('TAVILY_API_KEY', '')
WEB_SEARCH_GLOBAL_REQUESTS_PER_MINUTE = min(
    10000,
    max(1, int(os.environ.get('WEB_SEARCH_GLOBAL_REQUESTS_PER_MINUTE', '60'))),
)
WEB_SEARCH_GLOBAL_MONTHLY_REQUEST_LIMIT = min(
    1000000,
    max(1, int(os.environ.get('WEB_SEARCH_GLOBAL_MONTHLY_REQUEST_LIMIT', '1000'))),
)
BRAVE_SEARCH_GLOBAL_MONTHLY_REQUEST_LIMIT = min(
    1000000,
    max(1, int(os.environ.get('BRAVE_SEARCH_GLOBAL_MONTHLY_REQUEST_LIMIT', '800'))),
)
TAVILY_SEARCH_GLOBAL_MONTHLY_REQUEST_LIMIT = min(
    1000000,
    max(1, int(os.environ.get('TAVILY_SEARCH_GLOBAL_MONTHLY_REQUEST_LIMIT', '1000'))),
)
IMAGE_SEARCH_BULK_MAX_PRODUCTS = min(
    25,
    max(1, int(os.environ.get('IMAGE_SEARCH_BULK_MAX_PRODUCTS', '25'))),
)
IMAGE_SEARCH_TENANT_DAILY_JOBS = min(
    1000,
    max(1, int(os.environ.get('IMAGE_SEARCH_TENANT_DAILY_JOBS', '100'))),
)
PRODUCT_PARSE_TENANT_DAILY_JOBS = min(
    1000,
    max(1, int(os.environ.get('PRODUCT_PARSE_TENANT_DAILY_JOBS', '100'))),
)
WEB_RESEARCH_TENANT_DAILY_STARTS = min(
    300,
    max(1, int(os.environ.get('WEB_RESEARCH_TENANT_DAILY_STARTS', '30'))),
)
WEB_RESEARCH_AUTO_FALLBACK = os.environ.get(
    'WEB_RESEARCH_AUTO_FALLBACK', 'true',
).lower() in {'1', 'true', 'yes'}
WEB_RESEARCH_COVERAGE_THRESHOLD = min(
    1.0,
    max(0.0, float(os.environ.get('WEB_RESEARCH_COVERAGE_THRESHOLD', '0.65'))),
)
WEB_RESEARCH_MAX_QUERIES = min(
    10,
    max(1, int(os.environ.get('WEB_RESEARCH_MAX_QUERIES', '2'))),
)
WEB_RESEARCH_RESULTS_PER_QUERY = min(
    20,
    max(1, int(os.environ.get('WEB_RESEARCH_RESULTS_PER_QUERY', '8'))),
)

# Public URL transport uses direct DNS pinning when empty (development).
# Production settings require the exact trusted Squid endpoint instead.
PUBLIC_HTTP_PROXY_URL = os.environ.get('PUBLIC_HTTP_PROXY_URL', '').strip()

# --- Avito ---
AVITO_CLIENT_ID = os.environ.get('AVITO_CLIENT_ID', '')
AVITO_CLIENT_SECRET = os.environ.get('AVITO_CLIENT_SECRET', '')

# --- Биллинг ---
BILLING_ENABLED = os.environ.get('BILLING_ENABLED', 'false').strip().lower() in {
    '1', 'true', 'yes',
}
YOOKASSA_SHOP_ID = os.environ.get('YOOKASSA_SHOP_ID', '')
YOOKASSA_SECRET_KEY = os.environ.get('YOOKASSA_SECRET_KEY', '')
YOOKASSA_API_BASE_URL = os.environ.get(
    'YOOKASSA_API_BASE_URL',
    'https://api.yookassa.ru/v3',
)
YOOKASSA_API_CONNECT_TIMEOUT_SECONDS = min(
    30.0,
    max(0.1, float(os.environ.get('YOOKASSA_API_CONNECT_TIMEOUT_SECONDS', '3.05'))),
)
YOOKASSA_API_READ_TIMEOUT_SECONDS = min(
    60.0,
    max(0.1, float(os.environ.get('YOOKASSA_API_READ_TIMEOUT_SECONDS', '10'))),
)
YOOKASSA_API_MAX_ELAPSED_SECONDS = min(
    120.0,
    max(1.0, float(os.environ.get('YOOKASSA_API_MAX_ELAPSED_SECONDS', '30'))),
)
YOOKASSA_API_MAX_RESPONSE_BYTES = min(
    4 * 1024 * 1024,
    max(1024, int(os.environ.get('YOOKASSA_API_MAX_RESPONSE_BYTES', '1048576'))),
)
YOOKASSA_WEBHOOK_PROCESSING_TIMEOUT_SECONDS = min(
    3600,
    max(
        30,
        int(os.environ.get('YOOKASSA_WEBHOOK_PROCESSING_TIMEOUT_SECONDS', '120')),
    ),
)
YOOKASSA_WEBHOOK_RETRY_AFTER_SECONDS = min(
    3600,
    max(1, int(os.environ.get('YOOKASSA_WEBHOOK_RETRY_AFTER_SECONDS', '10'))),
)
YOOKASSA_RECONCILIATION_MAX_ATTEMPTS = min(
    1000,
    max(1, int(os.environ.get('YOOKASSA_RECONCILIATION_MAX_ATTEMPTS', '48'))),
)
YOOKASSA_RECONCILIATION_BASE_DELAY_SECONDS = min(
    3600,
    max(1, int(os.environ.get('YOOKASSA_RECONCILIATION_BASE_DELAY_SECONDS', '60'))),
)
YOOKASSA_RECONCILIATION_MAX_DELAY_SECONDS = min(
    86400,
    max(
        YOOKASSA_RECONCILIATION_BASE_DELAY_SECONDS,
        int(os.environ.get('YOOKASSA_RECONCILIATION_MAX_DELAY_SECONDS', '3600')),
    ),
)
YOOKASSA_RECONCILIATION_BATCH_SIZE = min(
    1000,
    max(1, int(os.environ.get('YOOKASSA_RECONCILIATION_BATCH_SIZE', '100'))),
)
BILLING_OUTBOX_BASE_DELAY_SECONDS = min(
    3600,
    max(1, int(os.environ.get('BILLING_OUTBOX_BASE_DELAY_SECONDS', '30'))),
)
BILLING_OUTBOX_MAX_DELAY_SECONDS = min(
    86400,
    max(
        BILLING_OUTBOX_BASE_DELAY_SECONDS,
        int(os.environ.get('BILLING_OUTBOX_MAX_DELAY_SECONDS', '3600')),
    ),
)
BILLING_OUTBOX_PROCESSING_TIMEOUT_SECONDS = min(
    3600,
    max(30, int(os.environ.get('BILLING_OUTBOX_PROCESSING_TIMEOUT_SECONDS', '300'))),
)
BILLING_OUTBOX_BATCH_SIZE = min(
    1000,
    max(1, int(os.environ.get('BILLING_OUTBOX_BATCH_SIZE', '100'))),
)
BILLING_OUTBOX_MAX_ATTEMPTS = min(
    1000,
    max(1, int(os.environ.get('BILLING_OUTBOX_MAX_ATTEMPTS', '25'))),
)
BILLING_CHECKOUT_MAX_KEYS_PER_INVOICE = min(
    100,
    max(1, int(os.environ.get('BILLING_CHECKOUT_MAX_KEYS_PER_INVOICE', '32'))),
)
YOOKASSA_ALLOW_TEST_PAYMENTS = os.environ.get(
    'YOOKASSA_ALLOW_TEST_PAYMENTS',
    'false',
).strip().lower() in ('1', 'true', 'yes')
BILLING_TRIAL_DAYS = min(
    365, max(1, int(os.environ.get('BILLING_TRIAL_DAYS', '14'))),
)
BILLING_GRACE_PERIOD_DAYS = min(
    90, max(0, int(os.environ.get('BILLING_GRACE_PERIOD_DAYS', '7'))),
)
SITE_URL = os.environ.get('SITE_URL', 'http://localhost:8000')
FRONTEND_URL = os.environ.get('FRONTEND_URL', 'http://localhost:3000')
MARKETPLACE_FEED_STORAGE_MODE = os.environ.get(
    'MARKETPLACE_FEED_STORAGE_MODE',
    'legacy_public',
).strip().lower()
if MARKETPLACE_FEED_STORAGE_MODE not in {
    'legacy_public', 'stable_bridge', 'private_generation',
}:
    raise ValueError(
        'MARKETPLACE_FEED_STORAGE_MODE must be legacy_public, stable_bridge '
        'or private_generation.',
    )
MARKETPLACE_FEED_URL_SIGNING_KEYS = parse_marketplace_feed_url_signing_keys(
    os.environ.get('MARKETPLACE_FEED_URL_SIGNING_KEYS', ''),
)
MARKETPLACE_FEED_URL_SIGNING_PRIMARY_KEY_ID = os.environ.get(
    'MARKETPLACE_FEED_URL_SIGNING_PRIMARY_KEY_ID',
    '',
).strip()
MARKETPLACE_FEED_PUBLIC_BASE_URL = (
    os.environ.get('MARKETPLACE_FEED_PUBLIC_BASE_URL', '').strip()
    or f'{SITE_URL.rstrip("/")}{PUBLIC_FEED_PATH}'
)
# This endpoint is deliberately outside the application failure domain.  It is
# a required production infrastructure provider, performs no bucket/object
# operation and lets a forward recovery validate DNS/TLS/egress while ingress
# for this application is unavailable.
PUBLIC_HTTP_PREFLIGHT_URL = 'https://storage.yandexcloud.net/'
BILLING_RETURN_URL_ALLOWED_ORIGINS = [
    origin.strip().rstrip('/')
    for origin in os.environ.get(
        'BILLING_RETURN_URL_ALLOWED_ORIGINS',
        FRONTEND_URL,
    ).split(',')
    if origin.strip()
]

# --- Уведомления ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_BOT_USERNAME = os.environ.get('TELEGRAM_BOT_USERNAME', '')  # без @, напр. MyMapBot
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'noreply@yourdomain.ru')
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.resend.com'
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = 'resend'
EMAIL_HOST_PASSWORD = os.environ.get('RESEND_API_KEY', '')

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
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 12},
    },
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# --- Поиск изображений ---
from config.settings.image_search import *  # noqa: E402, F403
