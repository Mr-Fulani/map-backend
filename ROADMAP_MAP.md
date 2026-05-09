# ROADMAP — Marketplace Automation Platform (MAP)
## Пошаговый план разработки | Phase 1: MVP (45 дней)

> Каждый этап строго зависит от предыдущего. Не переходить к следующему этапу, пока не выполнены все пункты чеклиста текущего.

---

## ЭТАП 0 — Инфраструктура и проект (Дни 1–3)

### Предусловия
- Нет (стартовый этап)

### Задачи

**День 1 — Репозиторий и локальное окружение**
- [ ] Создать GitHub Organization `map-platform`
- [ ] Создать репозиторий `map-backend` (private)
- [ ] Настроить ветки: `main` (protected), `develop` (protected)
- [ ] Создать `pyproject.toml` (или `requirements/base.txt`, `dev.txt`, `prod.txt`)
- [ ] Написать `Dockerfile` (Python 3.12-slim, non-root user)
- [ ] Написать `docker-compose.yml`:
  - `django` (команда: `python manage.py runserver 0.0.0.0:8000`)
  - `postgres:16-alpine` (с healthcheck)
  - `redis:7-alpine` (с healthcheck, maxmemory 512mb)
  - `celery_worker` (команда: `celery -A config worker -l info`)
  - `celery_beat` (команда: `celery -A config beat -S django_celery_beat.schedulers:DatabaseScheduler`)
- [ ] Создать `.env.example` (все переменные без значений)
- [ ] Добавить `.gitignore` (`.env`, `__pycache__`, `*.pyc`, `.DS_Store`, `media/`)
- [ ] Создать `Makefile` с командами: `up`, `down`, `shell`, `migrate`, `test`, `lint`

**День 2 — Django проект**
- [ ] `django-admin startproject config .`
- [ ] Разбить settings: `config/settings/base.py`, `development.py`, `production.py`
- [ ] Настроить `DATABASE_URL` через `dj-database-url`
- [ ] Настроить `REDIS_URL` через `django-redis`
- [ ] Создать структуру `apps/` (пустая папка с `__init__.py`)
- [ ] Настроить Celery в `config/celery.py`
- [ ] Настроить `django-celery-beat` (добавить в INSTALLED_APPS + миграции)
- [ ] Настроить Sentry SDK (`sentry_sdk.init(...)`)
- [ ] Настроить DRF: `DEFAULT_AUTHENTICATION_CLASSES`, `DEFAULT_PERMISSION_CLASSES`
- [ ] Настроить `drf-spectacular` (Swagger/OpenAPI): `schema_view` в `urls.py`

**День 3 — CI/CD и деплой**
- [ ] Написать `.github/workflows/ci.yml`:
  - `pytest --cov=apps --cov-fail-under=80`
  - `flake8 apps/`
  - `mypy apps/` (опционально на старте)
- [ ] Написать `.github/workflows/deploy.yml` (push в `main` → SSH на сервер → `docker compose pull && up -d && migrate`)
- [ ] Настроить сервер Timeweb Cloud: Docker, docker-compose, Nginx, certbot
- [ ] Получить SSL-сертификат для домена
- [ ] Проверить: `docker compose up` поднимает всё локально без ошибок

### ✅ Критерий завершения этапа 0
- [ ] `docker compose up` — все сервисы healthy
- [ ] `http://localhost:8000/api/docs/` — Swagger открывается
- [ ] `make test` — pytest запускается (0 тестов, 0 ошибок)
- [ ] GitHub Actions CI проходит на пустом проекте
- [ ] Прод-сервер отвечает по HTTPS

---

## ЭТАП 1 — Фундамент: Tenant, Auth, Base Models (Дни 4–8)

### Предусловия
- Этап 0 завершён полностью

### Создаваемые файлы
```
apps/core/          — базовые миксины, middleware
apps/tenants/       — Tenant, TenantUser, APIKey
apps/users/         — кастомный User
```

### Задачи

**День 4 — Core и базовые модели**
- [ ] Создать `apps/core/models.py`:
  ```python
  class TimestampedModel(models.Model):
      created_at = models.DateTimeField(auto_now_add=True)
      updated_at = models.DateTimeField(auto_now=True)
      class Meta:
          abstract = True

  class SoftDeleteModel(TimestampedModel):
      deleted_at = models.DateTimeField(null=True, blank=True)
      objects = SoftDeleteManager()  # исключает удалённые
      all_objects = models.Manager()
      def soft_delete(self): ...
      class Meta:
          abstract = True
  ```
- [ ] Создать кастомного пользователя `apps/users/models.py` (наследует `AbstractBaseUser`)
  - Поля: `email` (USERNAME_FIELD), `phone`, `is_active`, `is_staff`
  - Без `username`
- [ ] Зарегистрировать `AUTH_USER_MODEL = 'users.User'` в settings ДО первых миграций
- [ ] `python manage.py makemigrations users && migrate`

**День 5 — Tenant модели**
- [ ] Создать `apps/tenants/models.py`:
  - `Tenant` (name, slug, is_active, trial_ends_at, active_listings_count, sku_count, ai_credits_used)
  - `TenantUser` (tenant, user, role: owner/admin/operator/viewer)
  - `APIKey` (tenant, name, key_prefix, key_hash, is_active, last_used_at)
- [ ] Написать `APIKey.generate()` — создаёт ключ, возвращает plaintext один раз, хранит SHA256
- [ ] `makemigrations tenants && migrate`

**День 6 — TenantMiddleware и аутентификация**
- [ ] Создать `apps/core/middleware.py`:
  ```python
  class TenantMiddleware:
      def _resolve_tenant(self, request):
          # 1. По API Key из заголовка Authorization: Bearer map_sk_...
          # 2. По поддомену (slug.map.domain.ru)
          # 3. По сессии (для Django Admin)
  ```
- [ ] Добавить middleware в `MIDDLEWARE` (после `AuthenticationMiddleware`)
- [ ] Создать DRF Authentication: `APIKeyAuthentication` — проверяет SHA256 ключа
- [ ] Написать `TenantPermission` — проверяет `request.tenant` и роль пользователя

**День 7 — Сервисы и API tenants**
- [ ] Создать `apps/tenants/services.py`:
  - `TenantService.create_tenant(name, owner_email, owner_password) -> Tenant`
  - `TenantService.add_user(tenant, email, role) -> TenantUser`
  - `APIKeyService.create_key(tenant, name) -> (APIKey, plaintext)`
  - `APIKeyService.revoke_key(key_id, tenant)`
- [ ] Создать `apps/tenants/serializers.py` (TenantSerializer, TenantUserSerializer)
- [ ] Создать `apps/tenants/views.py` (RegisterView, TenantDetailView, APIKeyView)
- [ ] Добавить в Swagger

**День 8 — Тесты**
- [ ] `apps/tenants/tests/test_services.py`:
  - `test_create_tenant_creates_owner_role`
  - `test_api_key_hash_stored_not_plaintext`
  - `test_api_key_authentication_works`
  - `test_tenant_isolation` — запрос от тенанта A не видит данные тенанта B
- [ ] `apps/tenants/tests/test_middleware.py`:
  - `test_unknown_api_key_returns_403`
  - `test_inactive_tenant_returns_403`

### ✅ Критерий завершения этапа 1
- [ ] `POST /api/v1/auth/register/` — создаёт тенанта, пользователя, возвращает API Key
- [ ] Все запросы без валидного API Key → 403
- [ ] `request.tenant` доступен во всех views
- [ ] Тесты зелёные, покрытие tenants ≥ 80%
- [ ] CI зелёный

---

## ЭТАП 2 — Billing: Планы и подписки (Дни 9–11)

### Предусловия
- Этап 1 завершён: Tenant, APIKey, Auth работают

### Создаваемые файлы
```
apps/billing/models.py
apps/billing/services.py
apps/billing/serializers.py
apps/billing/views.py
apps/billing/tests/
```

### Задачи

**День 9 — Модели биллинга**
- [ ] Создать `Plan` (name, slug, price_monthly, price_yearly, limit_listings, limit_sku, limit_ai_credits, is_active)
- [ ] Создать `Subscription` (tenant OneToOne, plan, status, billing_period, period_start, period_end, yookassa_subscription_id, cancelled_at)
- [ ] Создать `Invoice` (tenant, amount, status, yookassa_payment_id, pdf_s3_key, paid_at)
- [ ] Создать `AIUsageLog` (tenant, date, credits_used) — для детального трекинга
- [ ] Загрузить начальные данные планов через `management command` или фикстуру
- [ ] `makemigrations billing && migrate`

**День 10 — LimitChecker и BillingService**
- [ ] `apps/billing/services.py`:
  ```python
  class LimitChecker:
      def can_publish(self, tenant) -> tuple[bool, str]: ...
      def can_import_sku(self, tenant, count) -> tuple[bool, str]: ...
      def can_generate_ai(self, tenant) -> tuple[bool, str]: ...
      def get_usage_summary(self, tenant) -> dict: ...

  class BillingService:
      def start_trial(self, tenant) -> Subscription: ...
      def upgrade_plan(self, tenant, plan, period) -> Subscription: ...
      def handle_payment_success(self, yookassa_event) -> None: ...
      def handle_payment_failed(self, yookassa_event) -> None: ...
      def check_expired_trials(self) -> None: ...  # Celery task
  ```
- [ ] Декоратор `@check_listing_limit` для Celery-задач публикации

**День 11 — API и тесты**
- [ ] `GET /api/v1/billing/plans/` — список тарифов
- [ ] `GET /api/v1/billing/subscription/` — текущая подписка тенанта
- [ ] `GET /api/v1/billing/usage/` — использование лимитов
- [ ] Тесты:
  - `test_starter_plan_blocks_at_1000_listings`
  - `test_trial_is_created_on_registration`
  - `test_grace_period_allows_existing_listings`

### ✅ Критерий завершения этапа 2
- [ ] При создании тенанта автоматически стартует Trial (Business, 14 дней)
- [ ] `LimitChecker.can_publish()` блокирует при превышении
- [ ] `GET /api/v1/billing/usage/` возвращает корректные данные
- [ ] Тесты зелёные

---

## ЭТАП 3 — Источники данных: 1С и CSV (Дни 12–19)

### Предусловия
- Этапы 1–2: Tenant, LimitChecker готовы

### Создаваемые файлы
```
apps/datasources/
├── models.py         — DataSourceConnection
├── base.py           — BaseDataSourceAdapter (ABC)
├── adapters/
│   ├── onec_http.py  — OneCHTTPAdapter
│   ├── onec_xml.py   — OneCXMLAdapter
│   └── csv_adapter.py
├── encryption.py     — Fernet шифрование credentials
├── services.py       — ConnectionService
├── views.py
└── tests/
```

### Задачи

**День 12 — Модель и шифрование**
- [ ] Создать `DataSourceConnection` (tenant, name, type, is_active, credentials BinaryField, last_sync_at, last_sync_status, last_error)
- [ ] Создать `apps/datasources/encryption.py`:
  ```python
  from cryptography.fernet import Fernet
  def encrypt(data: dict) -> bytes: ...
  def decrypt(data: bytes) -> dict: ...
  ```
  Ключ из `settings.FIELD_ENCRYPTION_KEY`
- [ ] `makemigrations datasources && migrate`

**День 13 — Базовый адаптер и регистрация**
- [ ] `apps/datasources/base.py` — `BaseDataSourceAdapter(ABC)`:
  - `fetch_changes(since, limit, offset) -> list[dict]`
  - `test_connection() -> bool`
  - `get_display_name() -> str`
- [ ] `apps/datasources/registry.py`:
  ```python
  ADAPTER_MAP = {
      '1c_http': OneCHTTPAdapter,
      '1c_xml':  OneCXMLAdapter,
      'csv':     CSVAdapter,
  }
  def get_adapter(connection: DataSourceConnection) -> BaseDataSourceAdapter: ...
  ```

**День 14–15 — OneCHTTPAdapter**
- [ ] Реализовать `OneCHTTPAdapter.fetch_changes()`:
  - GET запрос с `since`, `limit`, `offset` параметрами
  - Basic Auth из зашифрованных credentials
  - Timeout 30 сек
  - Retry 3 раза при 5xx
  - Возвращает нормализованный `list[dict]` (единый формат для всех адаптеров)
- [ ] Реализовать `OneCHTTPAdapter.test_connection()` — GET на `/ping` или первый запрос с limit=1
- [ ] Написать `OneCXMLAdapter` — разбор XML через `lxml`, конвертация в тот же dict-формат

**День 16–17 — CSVAdapter**
- [ ] `CSVAdapter`:
  - Принимает `file_path` (S3 key скачанного файла)
  - Поддерживает `.csv` и `.xlsx` (через `openpyxl`)
  - `REQUIRED_COLUMNS = ['article', 'name', 'price', 'stock_qty']`
  - Валидация: проверить обязательные колонки, типы данных
  - Нормализация: strip строк, Decimal для цены, int для qty
  - Возвращает `list[dict]` в том же формате что 1С-адаптеры
- [ ] `CSVAdapter.preview(file_path, rows=10) -> dict` — для UI предпросмотра

**День 18 — ConnectionService и API**
- [ ] `apps/datasources/services.py`:
  - `ConnectionService.create(tenant, data) -> DataSourceConnection`
  - `ConnectionService.test(connection_id, tenant) -> dict`
  - `ConnectionService.delete(connection_id, tenant)`
- [ ] API:
  - `GET/POST /api/v1/datasources/`
  - `GET/PUT/DELETE /api/v1/datasources/{id}/`
  - `POST /api/v1/datasources/{id}/test/`
  - `POST /api/v1/datasources/{id}/sync/` — ручной запуск
  - `POST /api/v1/datasources/upload-csv/` — загрузка файла

**День 19 — Тесты**
- [ ] Тест `OneCHTTPAdapter` через `responses` mock:
  - `test_fetch_changes_returns_normalized_format`
  - `test_retry_on_5xx`
  - `test_timeout_raises_exception`
- [ ] Тест `CSVAdapter`:
  - `test_valid_csv_parsed_correctly`
  - `test_missing_required_column_raises_error`
  - `test_xlsx_parsing`
- [ ] Тест шифрования credentials:
  - `test_credentials_stored_encrypted`
  - `test_plaintext_not_in_db`

### ✅ Критерий завершения этапа 3
- [ ] `POST /api/v1/datasources/{id}/test/` — возвращает `{"ok": true}` для 1С
- [ ] Загрузка CSV 1000 строк — парсится корректно, ошибки валидации понятны
- [ ] Credentials в БД — не читаются как текст при прямом SQL запросе
- [ ] Тесты зелёные, покрытие адаптеров ≥ 80%

---

## ЭТАП 4 — Продукты и файловое хранилище (Дни 20–24)

### Предусловия
- Этап 3: адаптеры нормализуют данные в единый формат

### Создаваемые файлы
```
apps/products/
├── models.py       — Product, ProductImage
├── services.py     — ProductService, SyncOrchestrator (черновик)
├── storage.py      — PhotoUploadPipeline (S3)
├── tasks.py        — import_products_task
└── tests/
```

### Задачи

**День 20 — Модель Product**
- [ ] Создать `Product` (все поля по ТЗ, + `tenant` FK обязательный)
  - `uuid_1c` — nullable (CSV не имеет UUID)
  - `unique_together = [('tenant', 'uuid_1c')]` — только если uuid_1c не null
  - `unique_together = [('tenant', 'datasource', 'article')]` — альтернативный ключ
  - Индексы: `(tenant, export_enabled, -sync_at)`, `(tenant, article)`
- [ ] Создать `ProductImage` (product, s3_key, s3_key_thumb, url_source, sha256, position)
  - `unique_together = [('product', 'sha256')]`
- [ ] `makemigrations products && migrate`

**День 21–22 — Yandex Cloud S3 и PhotoUploadPipeline**
- [ ] Настроить `django-storages` + `boto3` для Yandex Cloud:
  ```python
  STORAGES = {"default": {"BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
      "OPTIONS": {"endpoint_url": "https://storage.yandexcloud.net", ...}}}
  ```
- [ ] Создать `apps/products/storage.py` — `PhotoUploadPipeline`:
  - `process(source_url, product) -> ProductImage`
  - Скачать → проверить SHA256 (дедупликация) → Pillow ресайз 1280px → thumbnail 400px → загрузить 2 объекта в S3 → сохранить `ProductImage`
  - Лимит: максимум 10 фото на товар
  - Обработка ошибок: битое изображение, таймаут скачивания
- [ ] Тест загрузки с mock S3 (через `moto` или `responses`)

**День 23 — ProductService**
- [ ] `apps/products/services.py`:
  ```python
  class ProductService:
      @staticmethod
      def upsert_from_source(tenant, datasource, data: dict) -> tuple[Product, str]:
          """Создаёт или обновляет продукт. str = 'created'|'updated'|'unchanged'"""
          hash_new = compute_hash(data)
          product, created = Product.objects.update_or_create(
              tenant=tenant, ...
              defaults={...}
          )
          if created:
              return product, 'created'
          if product.hash_1c != hash_new:
              product.hash_1c = hash_new
              product.save()
              return product, 'updated'
          return product, 'unchanged'

      @staticmethod
      def detect_change_type(old_product, new_data) -> str:
          """Возвращает: 'price_only' | 'stock_only' | 'content' | 'category'"""
  ```

**День 24 — Celery задача импорта и тесты**
- [ ] `apps/products/tasks.py`:
  ```python
  @shared_task(bind=True, max_retries=3)
  def import_from_datasource(self, connection_id: int):
      """Запускается по расписанию или вручную."""
      connection = DataSourceConnection.objects.get(pk=connection_id)
      adapter = get_adapter(connection)
      since = connection.last_sync_at or (now() - timedelta(days=30))
      # Пагинация по 500 записей
      ...
  ```
- [ ] Тесты:
  - `test_upsert_creates_new_product`
  - `test_upsert_unchanged_product_not_updated`
  - `test_upsert_detects_price_change`
  - `test_photo_deduplication_by_sha256`
  - `test_photo_limit_10_per_product`

### ✅ Критерий завершения этапа 4
- [ ] Импорт 100 товаров из mock-1С за < 10 сек
- [ ] Фото загружаются в S3, оригинал и thumbnail существуют
- [ ] Повторный импорт тех же данных → 0 изменений (unchanged)
- [ ] `GET /api/v1/products/` — возвращает список товаров тенанта

---

## ЭТАП 5 — Маппинг категорий (Дни 25–26)

### Предусловия
- Этап 4: `Product.category_1c` заполняется при импорте

### Задачи

**День 25**
- [ ] Создать `CategoryMapping` (tenant, marketplace='avito', category_source, category_target, category_id, attributes_map, version)
- [ ] `apps/marketplaces/services.py` — `CategoryMappingService`:
  - `get_or_suggest(tenant, category_1c) -> CategoryMapping | None`
  - `bulk_create_from_dict(tenant, mappings: dict)`
- [ ] API:
  - `GET /api/v1/categories/unmapped/` — категории без маппинга (нужны для онбординга)
  - `GET/POST/PUT /api/v1/categories/mappings/`

**День 26**
- [ ] Загрузить актуальное дерево категорий Avito (JSON из официальной документации)
- [ ] Хранить как `AvitoCategory` (read-only, обновляется при изменении у Avito)
- [ ] Тесты: `test_unmapped_categories_returned_for_new_tenant`

### ✅ Критерий завершения этапа 5
- [ ] Новый тенант может увидеть свои категории из 1С и проставить маппинг через API
- [ ] Товар с замапленной категорией имеет `category_avito` и `category_id`

---

## ЭТАП 6 — AI-агент генерации описаний (Дни 27–31)

### Предусловия
- Этап 4: `Product` существует со всеми полями
- Этап 2: `LimitChecker.can_generate_ai()` работает

### Создаваемые файлы
```
apps/ai_agent/
├── prompts.py      — SYSTEM_PROMPT (полный текст)
├── services.py     — DescriptionAgent
├── validators.py   — валидация ответа агента
├── tasks.py        — generate_description_task
└── tests/
```

### Задачи

**День 27 — DescriptionAgent**
- [ ] `apps/ai_agent/services.py`:
  - `DescriptionAgent.generate(product, tenant, variation_index=0) -> dict`
  - `_call_claude(product, model) -> dict`
  - `_call_openai(product, model) -> dict` (fallback)
  - Инкремент `tenant.ai_credits_used` через `F()` атомарно
- [ ] `apps/ai_agent/prompts.py` — полный system prompt из ТЗ
- [ ] `apps/ai_agent/validators.py`:
  - `validate_title(title)` — длина 20–100
  - `validate_description(text)` — длина ≤ 7500, запрещённые слова (список)
  - `strip_contacts(text)` — regex удаление телефонов, email, ссылок
  - `validate_json_response(text) -> dict` — parse + все проверки

**День 28–29 — Кэширование и инвалидация**
- [ ] `DescriptionAgent` проверяет `listing.description_ai` — если есть и данные не изменились, не перегенерировать
- [ ] `ProductService.detect_change_type()` → решает нужна ли перегенерация:
  - `'price_only'` → не генерировать
  - `'stock_only'` → не генерировать
  - `'content'`, `'category'` → генерировать
- [ ] Сохранять `description_ai`, `ai_confidence`, `variation_index` в `Listing`

**День 30 — Celery задача**
- [ ] `apps/ai_agent/tasks.py`:
  ```python
  @shared_task(bind=True, max_retries=3, retry_backoff=True)
  def generate_description_task(self, product_id: int):
      product = Product.objects.get(pk=product_id)
      can, reason = LimitChecker().can_generate_ai(product.tenant)
      if not can:
          # Поставить listing в requires_review
          return
      result = DescriptionAgent().generate(product, product.tenant)
      # Сохранить в Listing.description_ai
  ```

**День 31 — Тесты**
- [ ] Использовать `unittest.mock` для mock Claude API (не тратить реальные кредиты)
- [ ] `test_generate_returns_valid_structure`
- [ ] `test_banned_words_trigger_retry`
- [ ] `test_fallback_to_openai_when_claude_fails`
- [ ] `test_ai_credits_incremented_atomically`
- [ ] `test_no_regeneration_on_price_change`

### ✅ Критерий завершения этапа 6
- [ ] 10 тестовых товаров → 10 корректных описаний (title 50–100, description ≤ 7500)
- [ ] confidence < 0.5 → статус `requires_review`
- [ ] Claude недоступен → fallback GPT-4o / GPT-4o-mini без исключения
- [ ] AI-кредиты учитываются per-tenant

---

## ЭТАП 7 — Avito API: Маркетплейс адаптер (Дни 32–41)

### Предусловия
- Этапы 4–6: Product, Listing (черновик), DescriptionAgent готовы
- Этап 5: CategoryMapping — хотя бы одна тестовая категория замаплена

### Создаваемые файлы
```
apps/marketplaces/
├── models.py           — MarketplaceAccount, Listing
├── base.py             — BaseMarketplaceAdapter
├── adapters/
│   └── avito/
│       ├── adapter.py  — AvitoAdapter
│       ├── auth.py     — AvitoAuthManager
│       ├── rate_limiter.py
│       └── error_handler.py
├── services.py         — ListingService
├── tasks.py            — publish/update/delete tasks
└── tests/
```

### Задачи

**День 32 — MarketplaceAccount и Listing**
- [ ] Создать `MarketplaceAccount` (tenant, marketplace, name, external_id, is_active, credentials_enc BinaryField, token_expires_at, requests_this_hour, hour_bucket_reset_at)
- [ ] Создать `Listing` (tenant, product, account, external_id, status, title, description_ai, ai_confidence, price_on_listing, publish_idempotency_key UUID, retry_count, next_retry_at, published_at)
- [ ] `unique_together = [('tenant', 'product', 'account')]`
- [ ] `makemigrations marketplaces && migrate`

**День 33 — AvitoAuthManager**
- [ ] `apps/marketplaces/adapters/avito/auth.py`:
  - `get_token(account) -> str` — из Redis cache или refresh
  - `_refresh_token(account) -> str` — POST на `https://api.avito.ru/token` (`grant_type=client_credentials`)
  - Хранить access token в Redis с TTL `(expires_in - 300)`
  - `credentials_enc` = Fernet-зашифрованный `{client_id, client_secret}` (refresh token отсутствует в client_credentials flow)

**День 34–35 — AvitoRateLimiter**
- [ ] Token-bucket per `(account_id, operation)` в Redis
- [ ] `consume(account, operation)` — блокирует если превышен
- [ ] `handle_response_headers(headers, account)` — адаптирует лимиты из `X-RateLimit-*`
- [ ] Консервативные стартовые лимиты: publish 10/min, update 30/min, price 60/min
- [ ] Записывает `SyncLog` с `event_type='rate_limit_hit'` при блокировке

**День 36–37 — AvitoAdapter (все операции)**
- [ ] `AvitoAdapter(account).publish(listing) -> str` (external_id)
- [ ] `AvitoAdapter(account).update(listing) -> None`
- [ ] `AvitoAdapter(account).update_price(listing) -> None`
- [ ] `AvitoAdapter(account).unpublish(listing) -> None`
- [ ] `AvitoAdapter(account).delete(listing) -> None`
- [ ] `AvitoAdapter(account).get_status(listing) -> dict`

**День 38 — Обработка ошибок Avito**
- [ ] `apps/marketplaces/adapters/avito/error_handler.py`:
  ```python
  def handle_avito_error(response, listing, account):
      code = response.status_code
      if code == 401: refresh_token_and_retry()
      elif code == 404: reset_external_id_and_republish()
      elif code == 409: handle_duplicate(listing)
      elif code == 413: compress_photos_and_retry()
      elif code == 422: mark_rejected(listing, response.json())
      elif code == 429: raise RateLimitError(backoff=get_backoff())
      elif code >= 500: raise ServerError(retry=True)
  ```

**День 39 — Celery задачи с идемпотентностью**
- [ ] `apps/marketplaces/tasks.py`:
  - `publish_listing_task(listing_id)` — Redis lock по `idempotency_key`
  - `update_listing_task(listing_id, change_type)` — только нужные поля
  - `update_price_task(listing_id)` — минимальный запрос
  - `unpublish_listing_task(listing_id)`
  - `delete_listing_task(listing_id)`
  - `check_moderation_task(listing_id)` — проверка статуса
- [ ] Все задачи через `transaction.on_commit(lambda: task.delay(...))`

**День 40 — ListingService и SyncOrchestrator**
- [ ] `apps/marketplaces/services.py` — `ListingService`:
  - `create_or_update_listing(product, change_type)`
  - Решает какую задачу поставить в Celery
- [ ] `apps/products/services.py` — дополнить `SyncOrchestrator`:
  - После `upsert_from_source()` → определить `change_type` → вызвать `ListingService`
  - Пагинация: 500 записей за раз, `last_sync_at` обновляется после

**День 41 — E2E тест + тесты**
- [ ] E2E с mock Avito: 10 товаров → 10 объявлений → mock 200 OK → `Listing.status = 'active'`
- [ ] `test_publish_idempotency` — дубль задачи не создаёт второй листинг
- [ ] `test_401_triggers_token_refresh`
- [ ] `test_404_triggers_republish`
- [ ] `test_429_applies_exponential_backoff`
- [ ] `test_price_change_calls_patch_not_put`

### ✅ Критерий завершения этапа 7
- [ ] E2E: товар из 1С → появляется листинг → задача в Celery → mock Avito возвращает 200 → `status='active'`
- [ ] Все 10 HTTP-кодов Avito обрабатываются без падения
- [ ] Дублирование задач не создаёт дублей объявлений
- [ ] Rate limiter логирует превышения

---

## ЭТАП 8 — Anti-ban система (Дни 42–44)

### Предусловия
- Этап 7: `publish_listing_task` работает, `MarketplaceAccount` существует

### Создаваемые файлы
```
apps/anti_ban/
├── ramp_up.py      — GradualRampUp
├── velocity.py     — VelocityController
├── shadow_ban.py   — ShadowBanDetector
└── tests/
```

### Задачи

**День 42 — GradualRampUp**
- [ ] `GradualRampUp.get_daily_limit(tenant) -> int`:
  ```python
  SCHEDULE = [(1,100),(3,250),(7,500),(14,2000),(30,10000)]
  # По tenant.created_at вычислить текущий день
  ```
- [ ] Интегрировать в `publish_listing_task` — проверять лимит перед публикацией
- [ ] `RampUpStatus` — отдельная запись per tenant для хранения текущего прогресса

**День 43 — VelocityController**
- [ ] Per-account счётчики в Redis (INCR + EXPIRE 3600):
  - `velocity:{account_id}:publish` → лимит 50/час
  - `velocity:{account_id}:update` → лимит 200/час
- [ ] При превышении: `SyncLog(event_type='anti_ban_trigger')` + задача ставится в очередь с задержкой

**День 44 — ShadowBanDetector**
- [ ] Celery задача `check_shadow_ban_task(account_id)` — запускается раз в день
- [ ] Если `total_views > 500 AND CTR < 0.5%` → WARNING уведомление тенанту
- [ ] (Опционально на этом этапе — данные из Avito Stats API могут быть недоступны в test)

### ✅ Критерий завершения этапа 8
- [ ] Новый тенант не может опубликовать более 100 объявлений в первый день
- [ ] Превышение velocity → задача откладывается, не падает
- [ ] `SyncLog` содержит записи `anti_ban_trigger`

---

## ЭТАП 9 — Уведомления и SyncLog (Дни 45–47)

### Предусловия
- Этапы 7–8: `SyncLog` создаётся в разных местах

### Создаваемые файлы
```
apps/notifications/
├── telegram.py     — TelegramNotifier
├── email.py        — EmailNotifier
├── services.py     — NotificationService
├── tasks.py
└── tests/
```

### Задачи

**День 45**
- [ ] `TenantNotificationSettings` (tenant OneToOne, telegram_chat_id, email, notify_on_error, notify_on_critical)
- [ ] `apps/notifications/telegram.py` — `TelegramNotifier.send(chat_id, message)`
- [ ] `apps/notifications/email.py` — через `django.core.mail.send_mail` или SendPulse SMTP

**День 46**
- [ ] `NotificationService.notify(tenant, level, message, payload)`:
  - `ERROR` → Telegram + Dashboard
  - `CRITICAL` → Telegram @mention + Email + Dashboard
  - `BILLING` → только Email
- [ ] `apps/notifications/tasks.py` — все уведомления асинхронно через Celery (очередь `notifications`)
- [ ] Шаблоны Telegram-сообщений по типам событий

**День 47**
- [ ] `GET /api/v1/logs/` — список SyncLog тенанта (фильтры: event_type, status, date)
- [ ] Celery Beat: `cleanup_old_logs` — ежедневно 02:00, удалять > 90 дней
- [ ] Тест: уведомление отправляется при `SyncLog(status='error')`

### ✅ Критерий завершения этапа 9
- [ ] Ошибка публикации → запись в SyncLog → Telegram-сообщение в mock
- [ ] `GET /api/v1/logs/` пагинирован и фильтруется

---

## ЭТАП 10 — Django Admin MVP (Дни 48–51)

### Предусловия
- Все предыдущие этапы завершены (данные есть, сервисы работают)

### Задачи

**День 48–49 — Установка django-unfold и базовый Admin**
- [ ] Установить `django-unfold`, настроить в `INSTALLED_APPS` до `django.contrib.admin`
- [ ] `apps/products/admin.py`:
  - `ProductAdmin`: list_display, list_filter, search_fields, actions
  - Actions: `force_publish_selected`, `force_archive_selected`, `regenerate_description_selected`
  - Inline: `ProductImageInline`
- [ ] `apps/marketplaces/admin.py`:
  - `ListingAdmin`: status, avito_url, rejection_reason, retry_count
- [ ] `apps/tenants/admin.py`:
  - `TenantAdmin`: name, plan, is_active, usage summary
- [ ] `apps/billing/admin.py`:
  - `SubscriptionAdmin`, `InvoiceAdmin`

**День 50 — Страница статистики**
- [ ] Кастомная Admin view `/admin/stats/` через `AdminSite.get_urls()`
- [ ] Выводит: кол-во активных тенантов, листингов по статусам, ошибок за 24 часа, глубина очередей Celery (через `inspect()`)

**День 51 — Расписание задач Celery Beat**
- [ ] Через django-admin интерфейс или management command создать PeriodicTask:
  - `sync_all_tenants` → каждые 5 минут
  - `check_moderation_status` → каждые 30 минут
  - `reconcile_listings` → ежедневно 03:00
  - `refresh_avito_stats` → каждый час
  - `cleanup_old_logs` → ежедневно 02:00
  - `billing_check_expired` → ежедневно 10:00
  - `update_tenant_counters` → каждые 15 минут

### ✅ Критерий завершения этапа 10
- [ ] `/admin/` — всё отображается корректно
- [ ] Оператор может вручную опубликовать/архивировать товар из Admin
- [ ] Все PeriodicTask активны, запускаются по расписанию

---

## ЭТАП 11 — YooKassa: реальный биллинг (Дни 52–55)

### Предусловия
- Этап 2: модели биллинга готовы
- Этап 9: уведомления работают (email при оплате)
- Есть тестовый магазин в YooKassa

### Задачи

**День 52–53**
- [ ] Установить `yookassa` SDK
- [ ] `BillingService.create_payment(tenant, plan, period) -> payment_url`
- [ ] `POST /api/v1/billing/checkout/` → redirect на YooKassa
- [ ] Webhook endpoint `POST /api/v1/billing/webhook/yookassa/`:
  - Верифицировать подпись (IP YooKassa + Secret Key)
  - `payment.succeeded` → `BillingService.handle_payment_success()`
  - `payment.canceled` → `BillingService.handle_payment_failed()`

**День 54**
- [ ] Grace period: Celery задача `billing_check_expired` — если `subscription.current_period_end < now()` и нет оплаты → `status='past_due'`, уведомление
- [ ] После 7 дней `past_due` → `status='cancelled'`, блокировка новых публикаций
- [ ] `GET /api/v1/billing/invoices/` — история платежей тенанта

**День 55**
- [ ] Тесты с mock YooKassa webhook:
  - `test_payment_succeeded_activates_subscription`
  - `test_payment_failed_sets_past_due`
  - `test_grace_period_allows_existing_listings`
  - `test_invalid_webhook_signature_returns_400`

### ✅ Критерий завершения этапа 11
- [ ] Тестовая оплата через YooKassa → `Subscription.status='active'`
- [ ] Email уведомление при успешной оплате
- [ ] Просроченная подписка блокирует новые публикации через 7 дней

---

## ЭТАП 12 — Нагрузочное тестирование и запуск (Дни 56–60)

### Предусловия
- Этапы 0–11 завершены

### Задачи

**День 56–57 — Нагрузочный тест**
- [ ] Написать скрипт: 50К товаров → mock 1С → полная выгрузка → замер времени
- [ ] Проверить: очередь Celery не теряет задачи при рестарте воркера
- [ ] Проверить: два тенанта по 25К → не влияют на данные друг друга

**День 58 — Тест отказоустойчивости**
- [ ] Отключить Redis → Django возвращает 500, воркеры ждут → Redis возвращается → очередь обрабатывается
- [ ] Отключить Avito (mock 503) 30 минут → задачи накапливаются → Avito возвращается → выполняются
- [ ] Рестарт Celery воркера в середине задачи → идемпотентность → нет дублей

**День 59 — Production hardening**
- [ ] Nginx: rate limiting по IP (`limit_req_zone`)
- [ ] Django: `SECURE_HSTS_SECONDS`, `SECURE_CONTENT_TYPE_NOSNIFF`, `X_FRAME_OPTIONS`
- [ ] PostgreSQL: настройка `pg_hba.conf`, только локальные подключения
- [ ] Настроить ротацию логов (logrotate)
- [ ] Backup script: `pg_dump | gzip | yc s3 cp`
- [ ] Проверить restore из бэкапа

**День 60 — Пилотный запуск**
- [ ] Первый реальный тенант: 500 товаров
- [ ] Gradual ramp-up: день 1 → max 100 объявлений
- [ ] Мониторинг Sentry: 0 критических ошибок
- [ ] Проверить: все метрики из ТЗ выполнены

### ✅ Критерий завершения MVP (Финальный)
- [ ] 500 реальных SKU опубликовано на Avito, ошибок < 1%
- [ ] Синхронизация цены ≤ 15 мин (замерить реально)
- [ ] Два независимых тенанта работают без конфликтов
- [ ] Бэкап и restore протестированы
- [ ] `make test` → покрытие ≥ 80%, CI зелёный

---

## PHASE 2 — SaaS Dashboard (Дни 61–90)

Начинать только после успешного пилота Phase 1.

| Этап | Содержание | Дней |
|------|-----------|------|
| 13 | Next.js проект, онбординг wizard (подключение Avito + источника данных) | 8 |
| 14 | Dashboard: главная (KPI), каталог товаров, карточка товара | 7 |
| 15 | Листинги, логи, настройки, биллинг-страницы | 6 |
| 16 | Аналитика: CTR, просмотры, ROI из Avito Stats API | 5 |
| 17 | Public API документация (Swagger + примеры), Webhook UI | 4 |
| **∑** | | **30 дней** |

---

## PHASE 3 — Второй маркетплейс (Дни 91–125)

| Этап | Содержание | Дней |
|------|-----------|------|
| 18 | Auto.ru адаптер (BaseMarketplaceAdapter → AutoRuAdapter) | 20 |
| 19 | Мультиплатформенный листинг (один товар → несколько маркетплейсов) | 8 |
| 20 | Расширенная аналитика, A/B тест заголовков | 7 |
| **∑** | | **35 дней** |