# RULEBOOK — Marketplace Automation Platform (MAP)
## Правила проекта для разработчика и AI-агента

> **Версия:** 1.0 | **Дата:** Май 2026
> **Разработчик:** 1 fullstack (бэкенд Python/Django)
> **AI-агент:** Claude — пишет код, ревьюит, отлаживает
> **Стек:** Django 5 · DRF · PostgreSQL 16 · Celery · Redis · Yandex Cloud S3

---

## ЧАСТЬ 1 — GIT И РЕПОЗИТОРИЙ

### 1.1 Структура репозитория
```
GitHub Organization: map-platform
└── map-backend    (Django + DRF, монорепо)
```

### 1.2 Стратегия веток
```
main      → продакшн. Только через PR. Деплоится автоматически.
develop   → интеграция. Сюда мержатся все фичи.
feature/* → новая функциональность   (feature/avito-adapter)
fix/*     → исправление багов        (fix/listing-duplicate-on-retry)
chore/*   → инфра, зависимости       (chore/add-yookassa-sdk)
```

**Правила:**
- `main` и `develop` защищены. Прямые пуши запрещены.
- Ветку создавать только от свежего `develop`.
- Называть понятно: `feature/tenant-middleware`, не `feature/wip`.
- Удалять ветку сразу после мержа (remote + local).

### 1.3 Формат коммитов (Conventional Commits)
```
<тип>(<scope>): <описание на русском, строчные, без точки>

feat(tenants): добавить аутентификацию по APIKey через SHA256
fix(avito): обработать 409 конфликт при дубле объявления
chore(docker): добавить healthcheck для celery beat
docs(api): задокументировать эндпоинты datasource в swagger
test(billing): добавить тесты крайних случаев LimitChecker
refactor(products): вынести вычисление хэша в utils
perf(sync): добавить составной индекс на export_enabled и sync_at
```

**Типы коммитов:**
- `feat` — новая функциональность
- `fix` — исправление бага
- `chore` — инфраструктура, зависимости, конфиг
- `docs` — документация
- `test` — только тесты
- `refactor` — рефакторинг без изменения поведения
- `perf` — оптимизация

**Правила:**
- Один коммит = одно логическое изменение.
- Не коммитить сломанный код в `develop`.
- Никогда не коммитить: `.env`, секреты, `__pycache__`, `*.pyc`.

### 1.4 Pull Request

- PR = фича готова + тесты написаны + CI зелёный.
- Название PR = название главного коммита.
- В описании PR: что сделано, как проверить локально.
- CI зелёный → мерж → удалить ветку → обновить local `develop`.

### 1.5 Обязательная последовательность после PR
```
PR готов
  → CI зелёный
    → merge в develop
      → удалить remote ветку
        → git fetch --prune && git checkout develop && git pull
          → git branch -d feature/...
            → новая ветка от свежего develop
```

Не переходить к следующему этапу ROADMAP пока вся цепочка не выполнена.

---

## ЧАСТЬ 2 — API КОНТРАКТ

### 2.1 Стандарт ответов — строго соблюдать

**Успех — список:**
```json
{
  "status": "ok",
  "data": [...],
  "meta": {
    "total": 1250,
    "page": 1,
    "page_size": 50,
    "next": "/api/v1/products/?page=2",
    "prev": null
  }
}
```

**Успех — объект:**
```json
{
  "status": "ok",
  "data": { ... }
}
```

**Ошибка валидации:**
```json
{
  "status": "error",
  "code": "validation_error",
  "errors": {
    "price": ["Ensure this value is greater than 0."],
    "article": ["This field is required."]
  }
}
```

**Системная ошибка:**
```json
{
  "status": "error",
  "code": "not_found",
  "message": "Product not found"
}
```

**Превышение лимита:**
```json
{
  "status": "error",
  "code": "plan_limit_reached",
  "message": "Active listings limit reached (1000/1000). Upgrade to Business plan.",
  "data": {
    "current": 1000,
    "limit": 1000,
    "upgrade_url": "/api/v1/billing/checkout/"
  }
}
```

### 2.2 Заголовки запроса
```
Authorization: Bearer <api_key>    # обязательный
X-Tenant-Slug: my-company          # опционально (если не по поддомену)
```

### 2.3 Версионирование
- Все эндпоинты: `/api/v1/...`
- Swagger всегда актуален: `http://localhost:8000/api/docs/`
- Изменение формата ответа → новая версия `/api/v2/...`, старая не ломается.

### 2.4 Порядок изменения API
```
1. Создать GitHub Issue с описанием изменения
2. Получить подтверждение (если фронт уже подключён)
3. Внести изменение
4. Обновить Swagger
```

---

## ЧАСТЬ 3 — СТАНДАРТЫ КОДА

### 3.1 Структура каждого Django app
```
apps/my_app/
├── __init__.py
├── models.py        # только модели и менеджеры
├── services.py      # вся бизнес-логика
├── serializers.py   # только сериализация/десериализация
├── views.py         # только роутинг + permissions
├── urls.py
├── admin.py
├── apps.py
├── tasks.py         # Celery задачи (если есть)
├── signals.py       # сигналы (по минимуму)
└── tests/
    ├── __init__.py
    ├── test_models.py
    ├── test_services.py
    └── test_api.py
```

### 3.2 Правило сервисного слоя
```python
# ✅ ПРАВИЛЬНО — логика только в services.py
class ListingService:
    @staticmethod
    @transaction.atomic
    def create_listing(product: Product, account: MarketplaceAccount) -> Listing:
        # Проверка лимитов
        can, reason = LimitChecker().can_publish(product.tenant)
        if not can:
            raise PlanLimitExceeded(reason)
        listing = Listing.objects.create(...)
        transaction.on_commit(lambda: publish_listing_task.delay(listing.pk))
        return listing

class ListingCreateView(CreateAPIView):
    def perform_create(self, serializer):
        ListingService.create_listing(
            product=get_object_or_404(Product, pk=..., tenant=self.request.tenant),
            account=...
        )

# ❌ НЕПРАВИЛЬНО — логика во view
class ListingCreateView(CreateAPIView):
    def post(self, request):
        if Listing.objects.filter(...).count() >= plan.limit:  # не здесь!
            ...
        listing = Listing.objects.create(...)               # не здесь!
```

### 3.3 Мультитенантная изоляция — ГЛАВНОЕ ПРАВИЛО

**Каждый queryset обязан фильтроваться по тенанту:**

```python
# ✅ ПРАВИЛЬНО
def get_queryset(self):
    return Product.objects.filter(tenant=self.request.tenant)

# ✅ ПРАВИЛЬНО — в сервисе
product = Product.objects.get(pk=product_id, tenant=tenant)

# ❌ КРИТИЧЕСКАЯ УЯЗВИМОСТЬ — никогда так
product = Product.objects.get(pk=product_id)  # утечка данных между тенантами!
```

**Правило:** если в `objects.get()` или `objects.filter()` нет `tenant=...` — это баг безопасности.

Исключения: только модели без тенанта (`Plan`, `AvitoCategory`).

### 3.4 Queryset правила
```python
# ✅ select_related и prefetch_related обязательны в list-views
Product.objects.filter(tenant=tenant, export_enabled=True)\
    .select_related('datasource')\
    .prefetch_related('images')\
    .only('id', 'name', 'article', 'price', 'stock_qty', 'sync_at')

# ✅ F() для атомарных числовых операций
Tenant.objects.filter(pk=tenant.pk).update(
    ai_credits_used=F('ai_credits_used') + 1
)

# ✅ bulk_create вместо цикла при массовых вставках
Product.objects.bulk_create([...], ignore_conflicts=True)

# ❌ N+1 — никогда
for product in products:
    print(product.avito_listing.status)   # N запросов!
```

### 3.5 Celery правила
```python
# ✅ Задача после коммита транзакции
@transaction.atomic
def create_listing(...):
    listing = Listing.objects.create(...)
    transaction.on_commit(
        lambda: publish_listing_task.delay(listing.pk)
    )

# ✅ Retry с backoff
@shared_task(bind=True, max_retries=3, retry_backoff=True,
             retry_backoff_max=300, autoretry_for=(TemporaryError,))
def publish_listing_task(self, listing_id: int): ...

# ✅ Идемпотентность — всегда проверять перед действием
def publish_listing_task(self, listing_id: int):
    listing = Listing.objects.select_for_update().get(pk=listing_id)
    if listing.external_id:
        return   # уже опубликовано, выходим
    with cache.lock(f'lock:publish:{listing.publish_idempotency_key}'):
        listing.refresh_from_db()
        if listing.external_id:
            return   # двойная проверка
        ...

# ❌ Никогда — задача внутри транзакции без on_commit
with transaction.atomic():
    listing.save()
    publish_listing_task.delay(listing.pk)   # может запуститься до коммита!
```

### 3.6 Модели — правила
```python
# ✅ Все модели наследуют TimestampedModel
# ✅ Мягкое удаление (SoftDeleteModel) для: Product, Listing, MarketplaceAccount
# ✅ on_delete=PROTECT для FK на бизнес-критичные объекты (Listing → Product)
# ✅ on_delete=SET_NULL + null=True для FK которые могут устареть
# ✅ Составные индексы на поля по которым фильтруем совместно
# ✅ unique_together с tenant для всех бизнес-объектов

# ❌ Никогда
class SomeModel(models.Model):   # без TimestampedModel
    tenant = ...
    # нет индекса на tenant — медленные запросы
```

### 3.7 Шифрование credentials
```python
# ✅ Credentials (1С пароли, OAuth токены) — только зашифрованные в BinaryField
connection.credentials = encrypt({'url': url, 'user': user, 'password': pwd})

# ❌ Никогда — plaintext в БД
connection.password = "secret123"   # видно в pg dump!
```

### 3.8 Тесты — правила
```python
# Минимальное покрытие: 80% для billing, tenants, avito, ai_agent, sync
# factory_boy для данных — не fixtures, не setUp с прямыми ORM вызовами
# Каждый тест изолирован, не зависит от порядка запуска
# Mock внешние API: Claude, OpenAI, Avito, YooKassa — никогда не вызывать в тестах

# Структура теста:
def test_publish_blocked_when_limit_reached():
    # Arrange
    tenant = TenantFactory(plan__limit_listings=10)
    ListingFactory.create_batch(10, tenant=tenant, status='active')
    product = ProductFactory(tenant=tenant)
    # Act & Assert
    with pytest.raises(PlanLimitExceeded):
        ListingService.create_listing(product=product, account=AccountFactory(tenant=tenant))
```

### 3.9 Язык документации и комментариев — Русский

**Вся документация и комментарии пишутся на русском языке.** Имена переменных, классов и функций — на английском (Python-стандарт).

```python
# ✅ ПРАВИЛЬНО — docstrings и комментарии на русском
class ListingService:
    """Сервис управления объявлениями на маркетплейсах."""

    @staticmethod
    def create_listing(product: Product, account: MarketplaceAccount) -> Listing:
        """
        Создаёт объявление для товара на указанном аккаунте.

        Проверяет лимиты плана перед публикацией.
        Задача публикации отправляется в Celery после коммита транзакции.

        Args:
            product: Товар для публикации.
            account: Аккаунт маркетплейса.

        Returns:
            Созданный объект Listing.

        Raises:
            PlanLimitExceeded: Если лимит объявлений исчерпан.
        """
        # Проверка лимитов тарифного плана
        can, reason = LimitChecker().can_publish(product.tenant)
        if not can:
            raise PlanLimitExceeded(reason)
        ...

# ✅ ПРАВИЛЬНО — инлайн-комментарии на русском
ttl = data['expires_in'] - 300   # Буфер 5 минут до истечения токена
cache.set(key, token, timeout=ttl)  # Кэш в Redis, не в БД

# ❌ НЕПРАВИЛЬНО — комментарии на английском
# Check plan limits before publishing
can, reason = LimitChecker().can_publish(tenant)
```

**Правила:**
- Docstrings для всех классов и публичных методов — обязательны, на **русском**
- Инлайн-комментарии к нетривиальной логике — на **русском**
- Коммиты — на **русском** (см. раздел 1.3)
- Имена переменных, классов, функций — на **английском** (Python PEP 8)
- Технические термины (Redis, Celery, S3, OAuth) можно не переводить

---

## ЧАСТЬ 4 — ИНСТРУКЦИИ ДЛЯ AI-АГЕНТА (Claude)

> Эта часть — правила для Claude при работе над MAP.

### 4.1 Контекст проекта

- **Проект:** SaaS B2B платформа автоматизации объявлений на маркетплейсах
- **MVP маркетплейс:** Avito (Автозапчасти)
- **Бэкенд:** Django 5 + DRF, PostgreSQL 16, Redis, Celery, Yandex Cloud S3
- **Мультитенантность:** row-level tenancy (каждая запись имеет `tenant` FK)
- **Адаптеры:** pluggable паттерн для источников данных и маркетплейсов
- **Биллинг:** YooKassa, тарифы по активным объявлениям
- **Хранилище:** Yandex Cloud Object Storage (S3-совместимый)
- **Серверы:** только Россия (Timeweb Cloud + Yandex Cloud)

### 4.2 Что Claude делает при каждом запросе

1. **Определяет этап** — из ROADMAP, в каком этапе мы находимся
2. **Проверяет предусловия** — не пишет код для следующего этапа если предыдущий не завершён
3. **Соблюдает tenant isolation** — каждый queryset фильтруется по `tenant=`
4. **Сервисный слой** — логика только в `services.py`, никогда во `views.py`
5. **Adapter pattern** — не хардкодит Avito-специфику в ядре
6. **Идемпотентность** — все Celery задачи с Redis lock + двойной проверкой
7. **Пишет тесты** — к каждому сервису базовые тесты (happy path + edge case)
8. **Проверяет N+1** — select_related/prefetch_related везде где нужно
9. **Указывает путь файла** — перед каждым блоком кода

### 4.3 Что Claude НЕ делает

- Не меняет архитектуру без обсуждения
- Не пишет бизнес-логику во views или serializers
- Не делает queryset без `tenant=` фильтра (кроме моделей-справочников)
- Не вызывает внешние API (Avito, Claude, YooKassa) в тестах напрямую — только mock
- Не добавляет зависимости без явного запроса
- Не переходит к следующему этапу ROADMAP если текущий не завершён
- Не игнорирует правила из RULEBOOK

### 4.4 Приоритеты при написании кода

```
1. Корректность (работает правильно, без edge case багов)
2. Безопасность (tenant isolation, encrypted credentials, no secrets in code)
3. Соответствие архитектуре (adapter pattern, service layer, idempotency)
4. Читаемость (понятные имена, комментарии к нетривиальной логике)
5. Производительность (не преждевременно — только если есть явная проблема)
```

### 4.5 Как Claude отвечает на вопросы по коду

```
1. Указывает путь файла: # apps/marketplaces/adapters/avito/adapter.py
2. Объясняет ПОЧЕМУ именно так (не просто "вот код")
3. Показывает полный рабочий код, не сниппеты с "..."
4. Указывает на потенциальные проблемы
5. Если несколько решений — объясняет компромиссы
6. Всегда добавляет базовый тест для написанного сервиса
```

### 4.6 Команды для сессии разработки

```
"Этап N — [название]"          → Claude фокусируется на этом этапе
"Делаем: [app/файл/функция]"   → конкретная задача
"Продолжаем с [место]"         → продолжение прерванной задачи
"Ревью кода: [код]"            → проверка на соответствие RULEBOOK
"Дебаг: [ошибка]"              → диагностика без изменения архитектуры
```

### 4.7 Чеклист перед отправкой кода (Claude проверяет всегда)

- [ ] Логика в `services.py`, не во `views.py`
- [ ] Каждый queryset фильтруется по `tenant=` (или это справочник)
- [ ] Celery задача запускается через `transaction.on_commit()`
- [ ] В Celery задаче: Redis lock + двойная проверка idempotency
- [ ] `select_related` / `prefetch_related` там где нужно
- [ ] `on_delete` корректный для каждого FK
- [ ] Credentials зашифрованы (не plaintext в БД)
- [ ] Внешний API вызов обёрнут в try/except с retry
- [ ] Написан хотя бы один тест
- [ ] Путь файла указан в комментарии

### 4.8 Специфические правила MAP

**Tenant isolation (критично):**
```python
# Перед КАЖДЫМ get() добавить tenant=
Product.objects.get(pk=pk, tenant=request.tenant)   # ✅
Product.objects.get(pk=pk)                           # ❌ уязвимость
```

**LimitChecker (перед публикацией):**
```python
# ВСЕГДА проверять лимиты перед публикацией
can, reason = LimitChecker().can_publish(tenant)
if not can:
    listing.status = 'limit_reached'
    listing.save()
    notify_tenant(tenant, 'warning', reason)
    return
```

**Adapter pattern (не ломать):**
```python
# Avito-специфика только внутри apps/marketplaces/adapters/avito/
# В SyncOrchestrator — только вызовы BaseMarketplaceAdapter
adapter = get_marketplace_adapter(account)  # не AvitoAdapter() напрямую!
adapter.publish(listing)
```

**Rate limiting (при каждом запросе к Avito):**
```python
# ПЕРЕД каждым запросом к Avito API
rate_limiter.consume(account, operation='publish')
# ПОСЛЕ ответа Avito
rate_limiter.handle_response_headers(response.headers, account)
```

---

## ЧАСТЬ 5 — ОКРУЖЕНИЕ И ИНФРАСТРУКТУРА

### 5.1 Серверы (только РФ)

| Сервис | Провайдер | Примечание |
|--------|----------|-----------|
| VPS | Timeweb Cloud | 4 vCPU, 8 GB RAM |
| S3 | Yandex Cloud Object Storage | ДЦ ru-central1 |
| CDN | Yandex Cloud CDN | для фото товаров |
| Email | SendPulse SMTP | российский провайдер |

### 5.2 Переменные окружения

Никогда не коммитить `.env`. Коммитить `.env.example`:

```bash
# Django
DJANGO_SECRET_KEY=your-secret-key-here
DJANGO_DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1

# PostgreSQL
DATABASE_URL=postgres://user:pass@localhost:5432/map_db

# Redis
REDIS_URL=redis://localhost:6379/0

# Yandex Cloud S3
YC_S3_BUCKET=map-media-prod
YC_S3_ACCESS_KEY=
YC_S3_SECRET_KEY=
YC_CDN_DOMAIN=cdn.map.yourdomain.ru

# AI
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# Avito OAuth
AVITO_CLIENT_ID=
AVITO_CLIENT_SECRET=

# Billing
YOOKASSA_SHOP_ID=
YOOKASSA_SECRET_KEY=

# Notifications
TELEGRAM_BOT_TOKEN=
SENDPULSE_SMTP_LOGIN=
SENDPULSE_SMTP_PASSWORD=
DEFAULT_FROM_EMAIL=noreply@yourdomain.ru

# Security
FIELD_ENCRYPTION_KEY=        # Fernet key, генерируется: Fernet.generate_key()
WEBHOOK_SIGNING_SECRET=      # HMAC для вебхуков

# Monitoring
SENTRY_DSN=
```

### 5.3 Docker правила

- `docker compose up` — поднимает всё (django, postgres, redis, celery_worker, celery_beat)
- `docker compose up -d postgres redis` — только БД без Django
- Никогда не использовать `latest` тег в prod Dockerfile
- Все сервисы имеют `healthcheck`
- Celery Beat использует `DatabaseScheduler` (расписание в PostgreSQL, не в коде)

### 5.4 Makefile команды

```makefile
make up          # docker compose up -d
make down        # docker compose down
make shell       # exec -it django bash
make migrate     # manage.py migrate
make migrations  # manage.py makemigrations
make test        # pytest --cov=apps
make lint        # flake8 apps/ + mypy apps/
make seed        # загрузить тестовые данные (планы, категории)
make backup      # pg_dump → gzip → Yandex Cloud S3
```

---

## ЧАСТЬ 6 — БЕЗОПАСНОСТЬ

### 6.1 Обязательные правила

| Правило | Как |
|--------|-----|
| Никаких секретов в коде | Только `.env`, проверка через `git secrets` |
| Credentials в БД — зашифрованы | Fernet encryption, ключ в `FIELD_ENCRYPTION_KEY` |
| API Keys — только SHA256 в БД | Plaintext показывается один раз |
| HTTPS везде | Let's Encrypt, `SECURE_HSTS_SECONDS = 31536000` |
| Логи без секретов | `scrub_secrets(event)` в Sentry before_send |
| 1С endpoint — только по IP | nginx `allow 1.2.3.4; deny all;` |

### 6.2 Чего никогда не делать

```python
# ❌ Секрет в коде
AVITO_SECRET = "hardcoded-secret"

# ❌ Plaintext credentials в БД
connection.password = CharField(...)

# ❌ Queryset без tenant
Product.objects.all()   # видно всем тенантам!

# ❌ Логирование секретов
logger.info(f"Avito token: {token}")

# ❌ Payload с токенами в SyncLog
SyncLog.objects.create(payload={'token': token, ...})  # хранить только метаданные
```

---

## ЧАСТЬ 7 — ЧЕКЛИСТ ЭТАПА (из ROADMAP)

### Этап считается завершённым когда:
- [ ] Код написан и соответствует ТЗ
- [ ] Все задачи из ROADMAP для этапа выполнены (✅)
- [ ] Тесты написаны, покрытие ≥ 80% для критических модулей
- [ ] `make test` — зелёный
- [ ] `make lint` — без ошибок (0 warnings flake8)
- [ ] CI зелёный на GitHub Actions
- [ ] PR апрувнут и смержен в `develop`
- [ ] Ветка удалена (remote + local)
- [ ] Swagger обновлён (новые эндпоинты видны)
- [ ] Критерии завершения из ROADMAP выполнены

---

## ЧАСТЬ 8 — ДИАГНОСТИКА ПРОБЛЕМ

### 8.1 Если что-то сломалось
```
1. Создать GitHub Issue: что делал → что ожидал → что получил → лог
2. Попросить Claude: "Дебаг: [текст ошибки + трейсбек]"
3. После исправления — добавить тест чтобы не повторилось
4. Коммит: fix(scope): описание что исправлено
```

### 8.2 Если непонятно как реализовать
```
1. Спросить Claude: "Как реализовать [задача]? Какие варианты?"
2. Claude объяснит варианты и компромиссы
3. Выбрать вариант → зафиксировать решение в комментарии к PR
```

### 8.3 Если Avito ведёт себя неожиданно
```
1. Залогировать полный request + response в SyncLog.payload
2. Проверить X-RateLimit-* заголовки в ответе
3. Если новый HTTP код — добавить обработку в error_handler.py
4. Обновить RULEBOOK если нужно изменить логику
```

---

## ЧАСТЬ 9 — ВНЕШНИЕ РЕСУРСЫ И ДОКУМЕНТАЦИЯ

### 9.1 Avito API

> **Правило:** перед реализацией любого нового метода Avito API — сверять с реальной документацией. Не писать по предположениям.

**Список всех OpenAPI спецификаций Avito:**
```bash
curl "https://developers.avito.ru/web/1/openapi/list"
```

**Скачать нужные спецификации (нужны для MAP):**
```bash
# Главный — публикация, обновление, удаление, статусы, статистика (/core/v1/ и /stats/v1/)
curl "https://developers.avito.ru/web/1/openapi/item" > avito-item-api.json

# OAuth токены
curl "https://developers.avito.ru/web/1/openapi/auth" > avito-auth-api.json

# Информация о пользователе/аккаунте
curl "https://developers.avito.ru/web/1/openapi/user" > avito-user-api.json
```

> ⚠️ Слага `core` и `stats` не существует — оба входят в слаг **`item`**.
> Слаг `item`: «API для получения статистики по объявлениям, применения дополнительных услуг,
> а также просмотр статусов объявлений».

**Просмотр через Swagger Editor:**
```
https://editor.swagger.io/ → File → Import URL → вставить URL спецификации
```

**Что обязательно проверять перед реализацией:**
- HTTP метод (GET / POST / PATCH)
- Точный URL и версия (`/core/v1/`, `/stats/v1/`, etc.)
- Тело запроса — имена полей (camelCase vs snake_case), обязательные параметры
- Структура ответа — вложенность, имена полей
- Rate limits и батч-лимиты (например, 200 itemIds за запрос для Stats API)
- Срок хранения данных (Stats API — 270 дней истории)

**Реализованные методы и что проверено:**

| Метод | Endpoint | Спека | Верифицирован |
|---|---|---|---|
| Публикация | `POST /core/v1/accounts/{id}/items` | `item` | частично |
| Обновление | `PUT /core/v1/accounts/{id}/items/{item_id}` | `item` | частично |
| Цена | `PATCH /core/v1/accounts/{id}/items/{item_id}` | `item` | частично |
| Статус | `GET /core/v1/accounts/{id}/items/{item_id}` | `item` | частично |
| Статистика | `POST /stats/v1/accounts/{id}/items` | `item` | ✅ по документации |

---

*RULEBOOK v1.0 — MAP. Май 2026.*
*Обновляется через PR при изменении архитектурных решений.*