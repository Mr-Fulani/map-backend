# ТЕХНИЧЕСКОЕ ЗАДАНИЕ
# Marketplace Automation Platform (MAP)
### SaaS B2B | Автозапчасти | v1.0

**Стек:** Django 5 · PostgreSQL 16 · Celery · Redis 7 · Yandex Cloud S3 · Next.js 14  
**Разработчик:** 1 fullstack-разработчик  
**MVP-маркетплейс:** Avito (Автозапчасти)  
**Масштаб:** до 50 000+ SKU на тенанта

---

## 1. ВВЕДЕНИЕ И ЦЕЛИ

### 1.1 Что строим

MAP — это мультитенантная B2B SaaS-платформа для автоматической синхронизации товарного каталога с Avito (и другими маркетплейсами в будущих версиях). Платформа обслуживает несколько независимых клиентов (тенантов) из единой инфраструктуры.

**Ключевое отличие от простого скрипта:** каждый клиент — изолированный тенант со своими аккаунтами Avito, источниками данных, тарифным планом и настройками.

### 1.2 Целевые клиенты

| Сегмент | Характеристика | Объём SKU |
|---------|---------------|-----------|
| Авторазборщики | б/у запчасти, нет 1С, загрузка CSV | 1К–20К |
| Оптовые дилеры | новые запчасти, 1С УТ 11.5, много SKU | 10К–100К |
| Агрегаторы | несколько складов/юрлиц, несколько Avito-аккаунтов | 50К–500К |

### 1.3 Ключевые метрики успеха

| Метрика | Цель |
|--------|------|
| Задержка синхронизации цены/остатка | ≤ 15 мин |
| Задержка публикации нового товара | ≤ 30 мин |
| Доля ошибок публикации | < 1% |
| Uptime платформы | ≥ 99.5% |
| Время до первой публикации (онбординг) | ≤ 60 мин |

---

## 2. БИЗНЕС-МОДЕЛЬ И МОНЕТИЗАЦИЯ

### 2.1 Рекомендуемая модель: подписка по активным объявлениям

Биллинг по количеству **активных объявлений на Avito** — это самая честная и понятная метрика для клиента: больше объявлений → больше продаж → готов платить больше.

### 2.2 Тарифные планы

| План | Активных объявлений | SKU в каталоге | AI-генераций/мес | Цена |
|------|-------------------|----------------|-----------------|------|
| **Starter** | до 1 000 | до 5 000 | 1 000 | 4 900 ₽/мес |
| **Business** | до 10 000 | до 30 000 | 5 000 | 14 900 ₽/мес |
| **Pro** | до 50 000 | до 150 000 | 20 000 | 34 900 ₽/мес |
| **Enterprise** | без лимита | без лимита | без лимита | от 79 900 ₽/мес |

**Дополнительно:** сверх включённых AI-генераций — 2 ₽/шт.  
**Trial:** 14 дней бесплатно, план Business, без ввода карты.  
**Скидка:** 20% при оплате за год.  
**Enterprise:** включает white-label, SLA, выделенного менеджера.

### 2.3 Ограничения по планам (enforcement)

```python
PLAN_LIMITS = {
    'starter':    {'active_listings': 1_000,  'sku': 5_000,   'ai_credits': 1_000},
    'business':   {'active_listings': 10_000, 'sku': 30_000,  'ai_credits': 5_000},
    'pro':        {'active_listings': 50_000, 'sku': 150_000, 'ai_credits': 20_000},
    'enterprise': {'active_listings': None,   'sku': None,    'ai_credits': None},
}
```

При достижении лимита: предупреждение за 10%, блокировка новых публикаций при 100%. Существующие объявления остаются активными.

---

## 3. АРХИТЕКТУРА

### 3.1 Высокоуровневая схема (multi-tenant)

```
Клиент A                    Клиент B                    Клиент C
(1С УТ 11.5)               (Excel/CSV)                 (1С + несколько складов)
     │                           │                            │
     └───────────────────────────┼────────────────────────────┘
                                 ▼
                    ┌─────────────────────────┐
                    │   MAP Django Backend    │
                    │  (мультитенантный)      │
                    │                         │
                    │  DataSource Adapters    │
                    │  ├── OneCHTTPAdapter    │
                    │  ├── OneCXMLAdapter     │
                    │  └── CSVAdapter         │
                    │                         │
                    │  Marketplace Adapters   │
                    │  └── AvitoAdapter       │  ← MVP
                    │      (AutoRuAdapter)    │  ← Phase 3
                    │                         │
                    │  AI Agent               │
                    │  Billing Module         │
                    │  Anti-ban System        │
                    └────────────┬────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                   ▼
        PostgreSQL 16         Redis 7         Yandex Cloud S3
        (основная БД)      (broker/cache)     (фото/медиа)
              │
     ┌────────┴────────┐
     ▼                 ▼
  Celery           Celery Beat
  Workers         (расписания,
                  в PostgreSQL)
              │
     ┌────────┴────────────┐
     ▼                     ▼
  Avito REST API     Claude / GPT-4o API
```

### 3.2 Принцип адаптеров (Pluggable Architecture)

**Источники данных** и **маркетплейсы** реализованы через абстрактные адаптеры. Добавление нового маркетплейса или нового источника данных — это новый класс, не правка ядра.

```python
# apps/datasources/base.py
class BaseDataSourceAdapter(ABC):
    def __init__(self, connection: 'DataSourceConnection'):
        self.connection = connection

    @abstractmethod
    def fetch_changes(self, since: datetime, limit: int = 500) -> list[dict]:
        """Возвращает изменённые товары начиная с since."""
        pass

    @abstractmethod
    def test_connection(self) -> bool:
        pass

# Реализации:
class OneCHTTPAdapter(BaseDataSourceAdapter): pass   # MVP, основной
class OneCXMLAdapter(BaseDataSourceAdapter): pass    # fallback
class CSVAdapter(BaseDataSourceAdapter): pass        # для клиентов без 1С
```

```python
# apps/marketplaces/base.py
class BaseMarketplaceAdapter(ABC):
    def __init__(self, account: 'MarketplaceAccount'):
        self.account = account

    @abstractmethod
    def publish(self, listing: 'Listing') -> str: pass      # → external_id
    @abstractmethod
    def update(self, listing: 'Listing') -> None: pass
    @abstractmethod
    def update_price(self, listing: 'Listing') -> None: pass
    @abstractmethod
    def unpublish(self, listing: 'Listing') -> None: pass
    @abstractmethod
    def delete(self, listing: 'Listing') -> None: pass
    @abstractmethod
    def get_status(self, listing: 'Listing') -> dict: pass

class AvitoAdapter(BaseMarketplaceAdapter): pass     # MVP
# class AutoRuAdapter(BaseMarketplaceAdapter): pass  # Phase 3
```

### 3.3 Технологический стек

| Категория | Технология | Версия | Обоснование |
|-----------|-----------|--------|-------------|
| Язык | Python | 3.12+ | |
| Веб-фреймворк | Django + DRF | 5.x / 3.15+ | Единый стек, без FastAPI |
| Очереди | Celery | 5.x | |
| Расписания | django-celery-beat | latest | Beat в PostgreSQL, per-tenant |
| Брокер | Redis | 7.x | |
| СУБД | PostgreSQL | 16 | |
| Файлы | django-storages + boto3 | latest | Yandex Cloud S3 |
| AI (основной, базовые тарифы) | Anthropic Claude API | claude-3-5-haiku-latest | В 10 раз дешевле Sonnet |
| AI (основной, тариф Pro) | Anthropic Claude API | claude-3-5-sonnet-latest | Для сложных генераций |
| AI (резерв) | OpenAI API | gpt-4o-mini | Fallback для базовых тарифов |
| AI (резерв, тариф Pro) | OpenAI API | gpt-4o | Fallback для тарифа Pro |
| Платежи | YooKassa SDK | latest | РФ рынок |
| Уведомления | Telegram Bot API | 7.x | |
| Email | SendPulse SMTP | — | Российский провайдер, без санкционных рисков |
| Мониторинг | Sentry + UptimeRobot | — | |
| Admin UI | django-unfold | latest | Красивый Django Admin |
| Dashboard | Next.js 14 (App Router) | 14 | Phase 2 |
| Контейнеры | Docker + docker-compose | latest | |
| CI/CD | GitHub Actions | — | |
| Веб-сервер | Nginx + Gunicorn | latest | |
| SSL | Let's Encrypt (certbot) | — | |

---

## 4. МОДЕЛИ ДАННЫХ

### 4.1 Tenant (Тенант / Организация)

```python
class Tenant(models.Model):
    name         = models.CharField(max_length=200)
    slug         = models.SlugField(unique=True)      # subdomain / API prefix
    plan         = models.ForeignKey('billing.Plan', on_delete=models.PROTECT)
    is_active    = models.BooleanField(default=True)
    trial_ends_at = models.DateTimeField(null=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    # Кэш счётчиков (обновляется Celery-задачей)
    active_listings_count = models.PositiveIntegerField(default=0)
    sku_count             = models.PositiveIntegerField(default=0)
    ai_credits_used       = models.PositiveIntegerField(default=0)

    class Meta:
        indexes = [Index(fields=['slug']), Index(fields=['is_active'])]
```

### 4.2 User и Roles

```python
class TenantUser(models.Model):
    ROLES = [('owner','Owner'), ('admin','Admin'),
             ('operator','Operator'), ('viewer','Viewer')]

    user   = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    role   = models.CharField(choices=ROLES, default='operator')

    class Meta:
        unique_together = [('user', 'tenant')]
```

**Матрица прав:**

| Действие | Owner | Admin | Operator | Viewer |
|---------|-------|-------|----------|--------|
| Управление тарифом / биллинг | ✅ | ❌ | ❌ | ❌ |
| Управление пользователями | ✅ | ✅ | ❌ | ❌ |
| Настройки подключений | ✅ | ✅ | ❌ | ❌ |
| Ручные действия (publish/archive) | ✅ | ✅ | ✅ | ❌ |
| Просмотр каталога и логов | ✅ | ✅ | ✅ | ✅ |

### 4.3 DataSourceConnection (Подключение к источнику данных)

```python
class DataSourceConnection(models.Model):
    TYPE_CHOICES = [('1c_http','1С HTTP'), ('1c_xml','1С XML'), ('csv','CSV/Excel')]

    tenant       = models.ForeignKey(Tenant, on_delete=models.CASCADE,
                                     related_name='datasource_connections')
    name         = models.CharField(max_length=200)   # "Склад Москва"
    type         = models.CharField(choices=TYPE_CHOICES)
    is_active    = models.BooleanField(default=True)

    # Зашифрованные credentials (Fernet)
    credentials  = models.BinaryField()   # encrypt(json({url, user, pass}))

    last_sync_at      = models.DateTimeField(null=True)
    last_sync_status  = models.CharField(max_length=20, default='never')
    last_error        = models.TextField(blank=True)
```

> **Безопасность:** credentials хранятся зашифрованными через `cryptography.fernet`. Ключ шифрования — в переменной окружения `FIELD_ENCRYPTION_KEY`.

### 4.4 MarketplaceAccount (Аккаунт Avito тенанта)

```python
class MarketplaceAccount(models.Model):
    MARKETPLACE_CHOICES = [('avito','Avito')]   # расширяемо

    tenant       = models.ForeignKey(Tenant, on_delete=models.CASCADE,
                                     related_name='marketplace_accounts')
    marketplace  = models.CharField(choices=MARKETPLACE_CHOICES, default='avito')
    name         = models.CharField(max_length=200)   # "Основной аккаунт"
    external_id  = models.CharField(max_length=100)   # user_id на Avito
    is_active    = models.BooleanField(default=True)

    # OAuth credentials (зашифрованы Fernet)
    # client_id + client_secret хранятся в credentials_enc
    # access_token кэшируется в Redis (TTL = expires_in - 300 сек)
    credentials_enc   = models.BinaryField()  # encrypt({client_id, client_secret})
    token_expires_at  = models.DateTimeField(null=True)  # информационно

    # Anti-ban: счётчики за текущий час
    requests_this_hour    = models.PositiveIntegerField(default=0)
    hour_bucket_reset_at  = models.DateTimeField(null=True)

    class Meta:
        unique_together = [('tenant', 'marketplace', 'external_id')]
```

### 4.5 Product (Номенклатура)

```python
class Product(models.Model):
    CONDITION_CHOICES = [('new','Новый'), ('used','Б/у')]

    tenant         = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    datasource     = models.ForeignKey(DataSourceConnection, on_delete=models.SET_NULL,
                                       null=True)

    # Идентификаторы
    uuid_1c        = models.UUIDField(null=True, blank=True)
    article        = models.CharField(max_length=100)
    cross_numbers  = ArrayField(CharField(max_length=50), default=list)
    oem_numbers    = ArrayField(CharField(max_length=50), default=list)

    # Контент
    name           = models.CharField(max_length=500)
    brand          = models.CharField(max_length=200)
    category_1c    = models.CharField(max_length=300, blank=True)
    condition      = models.CharField(choices=CONDITION_CHOICES, default='new')
    applicability  = models.JSONField(default=list)  # [{brand, model, years}]
    description_1c = models.TextField(blank=True)

    # Финансы
    price          = models.DecimalField(max_digits=12, decimal_places=2)
    stock_qty      = models.PositiveIntegerField(default=0)
    warehouse      = models.CharField(max_length=200, blank=True)

    # Управление выгрузкой
    export_enabled = models.BooleanField(default=False)
    sync_at        = models.DateTimeField(null=True)
    hash_1c        = models.CharField(max_length=64, blank=True)

    class Meta:
        unique_together = [('tenant', 'uuid_1c')]   # UUID уникален в рамках тенанта
        indexes = [
            Index(fields=['tenant', 'article']),
            Index(fields=['tenant', 'export_enabled', '-sync_at']),  # ← оптимизировано
        ]
```

### 4.6 ProductImage

```python
class ProductImage(models.Model):
    product      = models.ForeignKey(Product, on_delete=models.CASCADE,
                                     related_name='images')
    # S3 key (не полный URL — URL строится динамически)
    s3_key       = models.CharField(max_length=500)
    s3_key_thumb = models.CharField(max_length=500, blank=True)
    url_source   = models.URLField(blank=True)    # Откуда скачали (1С URL)
    sha256       = models.CharField(max_length=64, blank=True)  # дедупликация
    position     = models.PositiveSmallIntegerField(default=0)
    uploaded_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['position']
        unique_together = [('product', 'sha256')]   # нет дублей по содержимому
```

### 4.7 CategoryMapping (Маппинг категорий)

```python
class CategoryMapping(models.Model):
    tenant          = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    marketplace     = models.CharField(default='avito')
    category_source = models.CharField(max_length=300)   # из 1С или CSV
    category_target = models.CharField(max_length=200)   # категория Avito
    category_id     = models.IntegerField()
    attributes_map  = models.JSONField(default=dict)
    version         = models.PositiveSmallIntegerField(default=1)  # версионность

    class Meta:
        unique_together = [('tenant', 'marketplace', 'category_source')]
```

### 4.8 Listing (Объявление — обобщённое)

```python
class Listing(models.Model):
    STATUS_CHOICES = ['draft','pending','active','rejected',
                      'archived','deleted','requires_review','limit_reached']

    tenant      = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    product     = models.ForeignKey(Product, on_delete=models.CASCADE)
    account     = models.ForeignKey(MarketplaceAccount, on_delete=models.CASCADE)

    # Внешние данные
    external_id     = models.CharField(max_length=100, null=True, unique=True)
    external_url    = models.URLField(blank=True)
    status          = models.CharField(choices=STATUS_CHOICES, default='draft')
    rejection_reason = models.TextField(blank=True)

    # Контент
    title           = models.CharField(max_length=300)
    description_ai  = models.TextField()
    ai_confidence   = models.FloatField(null=True)
    price_on_listing = models.DecimalField(max_digits=12, decimal_places=2)

    # Идемпотентность
    publish_idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True)

    # Retry
    retry_count     = models.PositiveSmallIntegerField(default=0)
    next_retry_at   = models.DateTimeField(null=True)

    # Временные метки
    published_at    = models.DateTimeField(null=True)
    last_sync_at    = models.DateTimeField(null=True)

    class Meta:
        unique_together = [('tenant', 'product', 'account')]
        indexes = [
            Index(fields=['tenant', 'status']),
            Index(fields=['account', 'status']),
            Index(fields=['next_retry_at']),
        ]
```

### 4.9 SyncLog (Лог событий)

```python
class SyncLog(models.Model):
    EVENT_TYPES = [
        'datasource_import', 'description_gen', 'listing_publish',
        'listing_update', 'listing_price_update', 'listing_unpublish',
        'listing_delete', 'listing_error', 'moderation', 'billing_event',
        'anti_ban_trigger', 'rate_limit_hit',
    ]

    tenant     = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    product    = models.ForeignKey(Product, null=True, on_delete=models.SET_NULL)
    listing    = models.ForeignKey(Listing, null=True, on_delete=models.SET_NULL)
    event_type = models.CharField(choices=EVENT_TYPES)
    status     = models.CharField(choices=['ok','warn','error'])
    message    = models.TextField()
    payload    = models.JSONField(default=dict)   # request + response (без секретов)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            Index(fields=['tenant', '-created_at']),
            Index(fields=['tenant', 'event_type', 'status']),
        ]
        # Партиционирование по дате при росте >10M строк (Phase 4)
```

---

## 5. МОДУЛЬ АУТЕНТИФИКАЦИИ И МУЛЬТИТЕНАНТНОСТИ

### 5.1 Онбординг (путь нового клиента)

```
[Регистрация] → [Trial активирован] → [Мастер подключения]
     │                                         │
     ▼                                         ▼
  Email verify              ┌──────────────────────────────┐
                            │ Шаг 1: Подключить Avito      │
                            │   OAuth 2.0 flow             │
                            │                              │
                            │ Шаг 2: Источник данных       │
                            │   1С HTTP / XML / CSV        │
                            │                              │
                            │ Шаг 3: Тест подключения      │
                            │   Импорт 10 тестовых товаров │
                            │                              │
                            │ Шаг 4: Настройка категорий   │
                            │   Маппинг 1С → Avito         │
                            │                              │
                            │ Шаг 5: Первая синхронизация  │
                            │   [Запустить] → готово       │
                            └──────────────────────────────┘
```

### 5.2 API Keys для интеграций

```python
class APIKey(models.Model):
    tenant     = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    name       = models.CharField(max_length=100)   # "Production Key"
    key_prefix = models.CharField(max_length=8)     # "map_sk_X" — для отображения
    key_hash   = models.CharField(max_length=64)    # SHA256 от полного ключа
    is_active  = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
```

Полный ключ показывается **один раз** при создании, потом хранится только хэш.

---

## 6. МОДУЛЬ БИЛЛИНГА

### 6.1 Модели

```python
class Plan(models.Model):
    name               = models.CharField(max_length=50)   # 'Business'
    slug               = models.SlugField(unique=True)
    price_monthly      = models.DecimalField(max_digits=10, decimal_places=2)
    price_yearly       = models.DecimalField(max_digits=10, decimal_places=2)
    limit_listings     = models.PositiveIntegerField(null=True)
    limit_sku          = models.PositiveIntegerField(null=True)
    limit_ai_credits   = models.PositiveIntegerField(null=True)
    is_active          = models.BooleanField(default=True)

class Subscription(models.Model):
    STATUS = [('trial','Trial'), ('active','Active'),
              ('past_due','Past Due'), ('cancelled','Cancelled')]

    tenant          = models.OneToOneField(Tenant, on_delete=models.CASCADE)
    plan            = models.ForeignKey(Plan, on_delete=models.PROTECT)
    status          = models.CharField(choices=STATUS, default='trial')
    billing_period  = models.CharField(choices=[('monthly','Monthly'),
                                                ('yearly','Yearly')])
    current_period_start = models.DateField()
    current_period_end   = models.DateField()
    yookassa_subscription_id = models.CharField(max_length=200, blank=True)
    cancelled_at    = models.DateTimeField(null=True)

class Invoice(models.Model):
    tenant    = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    amount    = models.DecimalField(max_digits=10, decimal_places=2)
    status    = models.CharField(choices=[('pending','Pending'),
                                          ('paid','Paid'), ('failed','Failed')])
    yookassa_payment_id = models.CharField(max_length=200, blank=True)
    pdf_s3_key = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at    = models.DateTimeField(null=True)
```

### 6.2 Логика ограничений

```python
class LimitChecker:
    def can_publish(self, tenant: Tenant) -> tuple[bool, str]:
        """Проверяет перед публикацией."""
        if not tenant.subscription.status in ('trial', 'active'):
            return False, "Подписка неактивна"
        plan = tenant.plan
        if plan.limit_listings and tenant.active_listings_count >= plan.limit_listings:
            return False, f"Достигнут лимит {plan.limit_listings} объявлений"
        return True, ""

    def can_generate_ai(self, tenant: Tenant) -> tuple[bool, str]:
        if plan.limit_ai_credits and tenant.ai_credits_used >= plan.limit_ai_credits:
            return False, "AI-кредиты исчерпаны"
        return True, ""
```

### 6.3 Grace Period

- При неоплате: статус → `past_due`, письмо клиенту
- Grace period: 7 дней — синхронизация продолжается
- После 7 дней: статус → `cancelled`, новые публикации блокируются, существующие остаются
- После 30 дней без оплаты: уведомление об архивации данных (но не удаление)

### 6.4 Стратегия Downgrade

При смене плана на более дешёвый (например, Business → Starter):

1. **Проверка допустимости:** система проверяет `active_listings_count` и `sku_count` относительно лимитов нового плана
2. **Если лимиты не превышены:** downgrade применяется немедленно
3. **Если лимиты превышены:**
   - Downgrade откладывается до конца текущего оплаченного периода
   - Клиенту показывается предупреждение: «У вас 8 500 объявлений, лимит Starter — 1 000. Снимите лишние объявления или выберите другой план»
   - После окончания периода: новые публикации блокируются, существующие остаются активными (Avito сам снимает при истечении)
   - Система **не** удаляет объявления принудительно — клиент решает сам
4. **SKU в каталоге:** остаются без изменений, блокируется только `export_enabled` для превышающих лимит
5. **AI-кредиты:** счётчик сбрасывается на новый лимит с начала нового периода

---

## 7. МОДУЛЬ ИСТОЧНИКОВ ДАННЫХ

### 7.1 Адаптер 1С HTTP (основной)

```python
class OneCHTTPAdapter(BaseDataSourceAdapter):
    def fetch_changes(self, since: datetime, limit: int = 500) -> list[dict]:
        creds = decrypt(self.connection.credentials)
        response = requests.get(
            f"{creds['url']}/avito-sync/changes",
            params={'since': since.isoformat(), 'limit': limit},
            auth=(creds['user'], creds['password']),
            timeout=30
        )
        response.raise_for_status()
        return response.json()['items']
```

Формат ответа 1С — без изменений относительно исходного ТЗ (Раздел 4.2).

### 7.2 Адаптер XML (fallback для 1С)

```python
class OneCXMLAdapter(BaseDataSourceAdapter):
    def fetch_changes(self, since: datetime, limit: int = 500) -> list[dict]:
        # Скачивает XML с SFTP или локального пути
        # Парсит через lxml, конвертирует в тот же dict-формат
        pass
```

### 7.3 Адаптер CSV/Excel

```python
class CSVAdapter(BaseDataSourceAdapter):
    """Для клиентов без 1С. Файл загружается через UI."""

    REQUIRED_COLUMNS = ['article', 'name', 'price', 'stock_qty']
    OPTIONAL_COLUMNS = ['brand', 'category', 'condition', 'oem_numbers',
                        'cross_numbers', 'applicability', 'description']

    def process_uploaded_file(self, file_path: str) -> list[dict]:
        # pandas для CSV/XLSX
        # Валидация: проверить обязательные колонки
        # Нормализация: trim, тип цены, целое qty
        pass
```

**UI для CSV:** дропзона в Dashboard → предпросмотр первых 10 строк → маппинг колонок → импорт.

### 7.4 Инкрементальная синхронизация

```python
# apps/sync/services.py
class SyncOrchestrator:
    def run_for_tenant(self, tenant: Tenant) -> SyncResult:
        for connection in tenant.datasource_connections.filter(is_active=True):
            adapter = get_adapter(connection)
            since = connection.last_sync_at or (now() - timedelta(days=30))

            # Пагинация по 500 записей
            offset = 0
            while True:
                items = adapter.fetch_changes(since, limit=500, offset=offset)
                if not items:
                    break
                for item in items:
                    self._process_item(tenant, connection, item)
                offset += len(items)

            connection.last_sync_at = now()
            connection.save(update_fields=['last_sync_at'])

    def _process_item(self, tenant, connection, data: dict):
        hash_new = sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        product, created = Product.objects.update_or_create(
            tenant=tenant, uuid_1c=data.get('uuid'),
            defaults={...}
        )
        if created:
            schedule_publish.delay(product.id)
        elif product.hash_1c != hash_new:
            schedule_update.delay(product.id, detect_change_type(product, data))
```

---

## 8. МОДУЛЬ AI-ГЕНЕРАЦИИ

### 8.1 DescriptionAgent

```python
class DescriptionAgent:
    SYSTEM_PROMPT = """...(полный текст из исходного ТЗ, Раздел 8.1)..."""

    def generate(self, product: Product, tenant: Tenant) -> dict:
        # Проверить лимит кредитов
        can, reason = LimitChecker().can_generate_ai(tenant)
        if not can:
            raise AICreditsExhausted(reason)

        try:
            if tenant.plan.slug == 'pro':
                result = self._call_claude(product, model='claude-3-5-sonnet-latest')
            else:
                result = self._call_claude(product, model='claude-3-5-haiku-latest')
        except (anthropic.APIError, anthropic.RateLimitError):
            if tenant.plan.slug == 'pro':
                result = self._call_openai(product, model='gpt-4o')   # fallback
            else:
                result = self._call_openai(product, model='gpt-4o-mini')   # fallback

        # Инкрементировать счётчик
        Tenant.objects.filter(pk=tenant.pk).update(
            ai_credits_used=F('ai_credits_used') + 1
        )
        return result

    def _call_claude(self, product, model) -> dict:
        response = anthropic.messages.create(
            model=model,
            max_tokens=1000,
            system=self.SYSTEM_PROMPT,
            messages=[{'role': 'user', 'content': self._build_message(product)}]
        )
        return self._validate_and_parse(response.content[0].text)

    def _validate_and_parse(self, text: str) -> dict:
        data = json.loads(text)
        # Длина title
        if not (20 <= len(data['title']) <= 100):
            raise ValidationError("title out of range")
        # Длина description
        if len(data['description']) > 7500:
            data['description'] = self._truncate_at_paragraph(data['description'], 7500)
        # Запрещённые слова
        if self._has_banned_words(data['description']):
            raise BannedWordsError()
        # Контакты (regex)
        data['description'] = self._strip_contacts(data['description'])
        return data
```

### 8.2 Кэширование и инвалидация описаний

| Событие | Действие |
|---------|---------|
| Цена изменилась | Только `price_on_listing`, описание не трогаем |
| Остаток → 0 | Архивировать листинг, описание сохранить |
| Остаток 0 → >0 | Восстановить листинг, описание не перегенерировать |
| Название / применимость изменились | Перегенерировать описание |
| Категория изменилась | Перегенерировать + переопубликовать |
| Добавлены/удалены фото | Обновить фото, описание не трогать |
| confidence < 0.5 | Статус `requires_review`, оператор проверяет вручную |

---

## 9. МОДУЛЬ AVITO

### 9.1 Auth Manager (per-account)

```python
class AvitoAuthManager:
    TOKEN_KEY = 'avito:token:{account_id}'

    def get_token(self, account: MarketplaceAccount) -> str:
        token = cache.get(self.TOKEN_KEY.format(account_id=account.pk))
        if token:
            return token
        return self._refresh(account)

    def _refresh(self, account: MarketplaceAccount) -> str:
        creds = decrypt(account.credentials_enc)  # {client_id, client_secret}
        resp = requests.post('https://api.avito.ru/token', data={
            'grant_type': 'client_credentials',
            'client_id': creds['client_id'],
            'client_secret': creds['client_secret'],
        })
        data = resp.json()
        ttl = data['expires_in'] - 300   # буфер 5 минут
        cache.set(self.TOKEN_KEY.format(account_id=account.pk),
                  data['access_token'], timeout=ttl)
        return data['access_token']
```

### 9.2 Rate Limiting (реальный, per-account)

Лимиты Avito API не задокументированы публично — используем адаптивный подход:

```python
class AvitoRateLimiter:
    """Token bucket per MarketplaceAccount. Читает X-RateLimit-* из ответов."""

    def consume(self, account: MarketplaceAccount, operation: str) -> None:
        key = f'avito:rl:{account.pk}:{operation}'
        # Стартовые консервативные лимиты (уточняются в ходе работы)
        DEFAULTS = {
            'publish': {'rate': 10, 'per': 60},
            'update':  {'rate': 30, 'per': 60},
            'price':   {'rate': 60, 'per': 60},
            'delete':  {'rate': 10, 'per': 60},
        }
        # Реализация через Redis INCR + EXPIRE

    def handle_response_headers(self, headers: dict, account: MarketplaceAccount):
        """Обновляет лимиты на основе реальных заголовков Avito."""
        remaining = headers.get('X-RateLimit-Remaining')
        reset_at  = headers.get('X-RateLimit-Reset')
        if remaining and int(remaining) < 5:
            # Сбавить скорость для этого аккаунта
            cache.set(f'avito:rl:slow:{account.pk}', 1, timeout=int(reset_at))
```

### 9.3 Обработка всех HTTP-ошибок Avito

| Код | Причина | Действие системы |
|-----|--------|-----------------|
| 400 | Невалидные данные | Лог + Telegram alert, retry=0, статус `rejected` |
| 401 | Истёк токен | Обновить токен автоматически, retry через 5 сек |
| 403 | Нет прав | CRITICAL alert, остановить очередь аккаунта |
| 404 | Объявление не найдено | Сбросить external_id, переопубликовать |
| 409 | Конфликт (дубль) | Проверить по idempotency_key, залогировать |
| 413 | Фото слишком большое | Уменьшить до 1280px, повторить |
| 422 | Ошибка модерации | Сохранить rejection_reason, уведомить |
| 429 | Rate limit | Exponential backoff: 30s → 60s → 120s → 300s |
| 503 | Временная недоступность | Retry 3 раза, затем CRITICAL alert |
| 5xx | Ошибка сервера Avito | Retry 3 раза с backoff, затем alert |

### 9.4 Идемпотентность публикации

```python
@shared_task(bind=True, max_retries=3)
def publish_listing(self, listing_id: int):
    listing = Listing.objects.select_for_update().get(pk=listing_id)

    # Идемпотентная проверка: уже опубликовано?
    if listing.external_id:
        return

    # Распределённая блокировка через Redis
    lock_key = f'avito:publish_lock:{listing.publish_idempotency_key}'
    with cache.lock(lock_key, timeout=60):
        # Двойная проверка после захвата блокировки
        listing.refresh_from_db()
        if listing.external_id:
            return

        can, reason = LimitChecker().can_publish(listing.tenant)
        if not can:
            listing.status = 'limit_reached'
            listing.save()
            return

        try:
            external_id = AvitoAdapter(listing.account).publish(listing)
            listing.external_id = external_id
            listing.status = 'active'
            listing.published_at = now()
        except Exception as exc:
            listing.retry_count += 1
            raise self.retry(exc=exc, countdown=backoff(listing.retry_count))
        finally:
            listing.save()
```

### 9.5 Celery-очереди

| Очередь | Воркеры | Содержание | Приоритет |
|---------|--------|-----------|-----------|
| `sync_import` | 2 | Импорт из источников данных | HIGH |
| `avito_publish` | 2 | Создание объявлений | NORMAL |
| `avito_update` | 3 | Обновление контента | NORMAL |
| `avito_price` | 5 | Только изменение цены | HIGH |
| `avito_delete` | 1 | Удаление/архивация | LOW |
| `ai_generate` | 3 | Генерация описаний | NORMAL |
| `notifications` | 1 | Telegram + Email | HIGH |
| `billing` | 1 | Биллинг-события | HIGH |

> **MVP (один VPS, 4 vCPU):** один `celery_worker` с `--concurrency=4`, подписанный на все очереди через приоритеты (`-Q sync_import,avito_price,notifications,billing,avito_publish,avito_update,ai_generate,avito_delete`). Количество воркеров в таблице выше — целевая **prod-конфигурация** при масштабировании (≥3 серверов). Переход — при устойчивой нагрузке >5 тенантов.

### 9.6 Расписание задач (django-celery-beat, хранится в БД)

| Задача | Расписание | Описание |
|--------|-----------|---------|
| `sync_all_tenants` | Каждые 5 мин | Запускает импорт для всех активных тенантов |
| `check_moderation_status` | Каждые 30 мин → 2 часа после активации | Адаптивно |
| `reconcile_listings` | Ежедневно 03:00 | Сверка БД vs Avito по тенантам |
| `refresh_avito_stats` | Каждый час | CTR, просмотры из Avito Stats API |
| `cleanup_old_logs` | Ежедневно 02:00 | Удалить SyncLog старше 90 дней |
| `health_check` | Каждый час | Проверить Avito API + 1С доступность |
| `billing_charge` | Ежедневно 10:00 | Попытка списания по просроченным подпискам |
| `update_tenant_counters` | Каждые 15 мин | Пересчёт active_listings_count, sku_count |

---

## 10. ANTI-BAN СИСТЕМА

### 10.1 Gradual Ramp-Up (при первом запуске тенанта)

```python
RAMP_UP_SCHEDULE = [
    # (день, макс. объявлений за день)
    (1,  100),
    (2,  250),
    (3,  500),
    (7,  2_000),
    (14, 10_000),
    (30, 'unlimited'),
]
```

Система автоматически рассчитывает текущий лимит по `tenant.created_at`.

### 10.2 Velocity Control (per-account)

```python
class VelocityController:
    HOURLY_LIMITS = {
        'publish': 50,    # новых объявлений в час
        'update':  200,   # обновлений в час
        'delete':  30,    # удалений в час
    }

    def is_allowed(self, account: MarketplaceAccount, operation: str) -> bool:
        key = f'velocity:{account.pk}:{operation}:{hour_bucket()}'
        count = cache.incr(key)
        if count == 1:
            cache.expire(key, 3600)
        if count > self.HOURLY_LIMITS[operation]:
            SyncLog.objects.create(event_type='anti_ban_trigger', ...)
            return False
        return True
```

### 10.3 Текстовые вариации (Anti-duplicate)

```python
class DescriptionAgent:
    def generate_with_variation(self, product, variation_index: int = 0) -> dict:
        """
        Avito детектирует дубли по тексту.
        При переопубликации используем другую вариацию.
        """
        variant_prompt = self.SYSTEM_PROMPT + f"""
        === ВАРИАНТ ОПИСАНИЯ ===
        Это вариант #{variation_index + 1}. Используй другую структуру первого абзаца
        и другой порядок характеристик по сравнению с предыдущими версиями.
        """
        # variation_index сохраняется в Listing
```

### 10.4 Shadow Ban Detection

```python
class ShadowBanDetector:
    """Анализирует статистику Avito для детекции теневого бана."""

    def check_account(self, account: MarketplaceAccount):
        stats = AvitoAdapter(account).get_stats(days=7)

        avg_ctr = stats['total_clicks'] / max(stats['total_views'], 1)
        if stats['total_views'] > 500 and avg_ctr < 0.005:
            # CTR < 0.5% при >500 просмотрах — аномалия
            notify_tenant(account.tenant,
                level='warning',
                message=f"Возможный shadow ban аккаунта {account.name}. "
                        f"CTR: {avg_ctr:.1%} за 7 дней")
```

---

## 11. ФАЙЛОВОЕ ХРАНИЛИЩЕ (Yandex Cloud S3)

### 11.1 Конфигурация django-storages

```python
# settings/base.py
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
        "OPTIONS": {
            "endpoint_url": "https://storage.yandexcloud.net",
            "region_name": "ru-central1",
            "bucket_name": env("YC_S3_BUCKET"),
            "access_key": env("YC_S3_ACCESS_KEY"),
            "secret_key": env("YC_S3_SECRET_KEY"),
            "file_overwrite": False,
            "default_acl": "private",
            "custom_domain": env("YC_CDN_DOMAIN", default=None),
        },
    },
}
```

### 11.2 Pipeline загрузки фото

```python
class PhotoUploadPipeline:
    MAX_SIZE = (1280, 1280)
    THUMB_SIZE = (400, 400)
    FORMAT = 'JPEG'
    QUALITY = 85

    def process(self, source_url: str, product: Product) -> ProductImage:
        # 1. Скачать
        raw = requests.get(source_url, timeout=15).content

        # 2. Дедупликация по SHA256
        sha = sha256(raw).hexdigest()
        existing = ProductImage.objects.filter(product=product, sha256=sha).first()
        if existing:
            return existing

        # 3. Ресайз через Pillow
        img = Image.open(BytesIO(raw))
        img_resized = self._resize(img, self.MAX_SIZE)
        thumb = self._resize(img, self.THUMB_SIZE)

        # 4. Загрузить в S3
        s3_key = f"products/{product.tenant_id}/{product.pk}/{sha[:8]}.jpg"
        s3_key_thumb = f"products/{product.tenant_id}/{product.pk}/{sha[:8]}_thumb.jpg"
        default_storage.save(s3_key, self._to_bytes(img_resized))
        default_storage.save(s3_key_thumb, self._to_bytes(thumb))

        # 5. Сохранить запись (не хранить URL — строить динамически)
        return ProductImage.objects.create(
            product=product, s3_key=s3_key,
            s3_key_thumb=s3_key_thumb,
            url_source=source_url, sha256=sha
        )
```

**Лимит:** максимум 10 фото на товар (лимит Avito). Загружаются по позиции.

---

## 12. УВЕДОМЛЕНИЯ И ЛОГИРОВАНИЕ

### 12.1 Уровни и каналы

| Уровень | Событие | Telegram | Email | Dashboard |
|---------|---------|---------|-------|-----------|
| INFO | Успешная публикация | ❌ | ❌ | ✅ |
| WARNING | Retry, low confidence, shadow ban | ❌ | ❌ | ✅ |
| ERROR | Ошибка публикации, 1С недоступна | ✅ | ❌ | ✅ |
| CRITICAL | Auth failure, >10 ошибок/час, бан | ✅ @mention | ✅ | ✅ |
| BILLING | Оплата, лимиты | ❌ | ✅ | ✅ |

### 12.2 Per-tenant Telegram

Каждый тенант настраивает свой Telegram chat_id в настройках. Бот — единый для платформы, сообщения маршрутизируются по chat_id тенанта.

### 12.3 Sentry

```python
import sentry_sdk
sentry_sdk.init(
    dsn=env("SENTRY_DSN"),
    traces_sample_rate=0.1,
    before_send=lambda event, hint: scrub_secrets(event),  # убрать API ключи
)
```

---

## 13. PUBLIC API И ВЕБХУКИ

### 13.1 REST API (DRF)

Аутентификация: `Authorization: Bearer <api_key>`

| Метод | Endpoint | Описание |
|-------|---------|---------|
| GET | `/api/v1/products/` | Список товаров (фильтры, пагинация) |
| GET | `/api/v1/products/{id}/` | Детали товара |
| POST | `/api/v1/products/{id}/publish/` | Принудительная публикация |
| POST | `/api/v1/products/{id}/archive/` | Принудительная архивация |
| POST | `/api/v1/products/{id}/regenerate/` | Перегенерировать описание |
| GET | `/api/v1/listings/` | Список листингов с фильтрами |
| GET | `/api/v1/logs/` | Лог событий |
| GET | `/api/v1/stats/` | Сводная статистика |
| GET | `/api/v1/stats/sync-health/` | Состояние синхронизации |
| POST | `/api/v1/datasources/{id}/sync/` | Ручной запуск импорта |
| GET | `/api/v1/billing/usage/` | Использование лимитов тенантом |

Rate limiting API: по тарифу (`X-RateLimit-*` в ответах).

### 13.2 Webhook Events

```python
WEBHOOK_EVENTS = [
    'listing.published',       # объявление опубликовано
    'listing.updated',         # обновлено
    'listing.rejected',        # отклонено модерацией
    'listing.archived',        # снято
    'sync.completed',          # импорт завершён
    'sync.failed',             # импорт упал
    'limit.reached',           # достигнут лимит плана
    'billing.payment_success', # оплата прошла
    'billing.payment_failed',  # оплата не прошла
]
```

Доставка: HTTPS POST на URL тенанта, подпись `X-MAP-Signature: HMAC-SHA256`, retry 3 раза с backoff при неудаче, лог попыток.

---

## 14. ФРОНТЕНД

### 14.1 Phase 1 — Django Admin + django-unfold (MVP)

Срок: **3–4 дня** вместо 12 дней на Next.js.

```python
# apps/products/admin.py
@admin.register(Product)
class ProductAdmin(ModelAdmin):  # django-unfold
    list_display  = ['name', 'article', 'price', 'stock_qty',
                     'export_enabled', 'listing_status', 'sync_at']
    list_filter   = ['export_enabled', 'condition', 'avito_listing__status']
    search_fields = ['name', 'article', 'oem_numbers', 'cross_numbers']
    actions       = ['force_publish', 'force_archive', 'regenerate_description']

    @admin.action(description='Опубликовать на Avito')
    def force_publish(self, request, queryset):
        for product in queryset:
            publish_listing.delay(product.avito_listing.pk)
```

Возможности Admin MVP: таблица товаров с фильтрами, ручные действия, лог событий, статистика (простая страница с QuerySet), управление категориями.

### 14.2 Phase 2 — Next.js 14 Dashboard (после первых клиентов)

| Страница | URL | Приоритет |
|---------|-----|-----------|
| Главный дашборд | `/` | P1 |
| Каталог товаров | `/products` | P1 |
| Карточка товара | `/products/[id]` | P1 |
| Листинги Avito | `/listings` | P1 |
| Лог событий | `/logs` | P2 |
| Аналитика | `/analytics` | P2 |
| Настройки | `/settings` | P1 |
| Биллинг | `/billing` | P1 |
| Онбординг wizard | `/onboarding` | P1 |

**Стек Phase 2:** Next.js 14 App Router · shadcn/ui · Tailwind · TanStack Query · TanStack Table · Recharts · NextAuth.js

---

## 15. БЕЗОПАСНОСТЬ

| Требование | Реализация |
|-----------|-----------|
| Secrets в коде | Только в `.env`, никогда в git |
| Credentials в БД | Шифрование Fernet (`cryptography`) |
| API Keys | Только SHA256 в БД, plaintext — один раз |
| HTTPS | Let's Encrypt, HSTS заголовки |
| 1С endpoint | IP-whitelist или VPN |
| Мультитенантная изоляция | Все запросы фильтруются по `tenant=request.tenant` |
| Middleware tenant scope | `TenantMiddleware` — автоматически добавляет tenant в каждый запрос |
| Логи | Без API-ключей, без персональных данных покупателей |
| 152-ФЗ | Данные тенантов только на серверах в РФ (Yandex Cloud, Hetzner Frankfurt — только если нет ПД физлиц) |
| Аудит | `AuditLog` для всех действий пользователей (кто, что, когда) |

```python
# apps/core/middleware.py
class TenantMiddleware:
    def __call__(self, request):
        # Определить тенант по поддомену или API Key
        tenant = self._resolve_tenant(request)
        if not tenant or not tenant.is_active:
            return HttpResponse(status=403)
        request.tenant = tenant
        return self.get_response(request)
```

---

## 16. ИНФРАСТРУКТУРА

### 16.1 Сервисы

Все сервисы — только российские дата-центры (152-ФЗ + отсутствие санкционных рисков).

| Сервис | Рекомендация | Параметры | Цена/мес |
|--------|-------------|----------|---------|
| VPS (основной) | **Timeweb Cloud** / Selectel | 4 vCPU, 8 GB RAM, 80 GB SSD | ~2 000–3 000 ₽ |
| БД Managed | **Timeweb Managed PostgreSQL 16** | При росте — перейти с self-hosted | ~1 500 ₽ |
| Redis Managed | **Timeweb Managed Redis 7** | При росте | ~800 ₽ |
| S3 | **Yandex Cloud Object Storage** | S3-совместимый, ДЦ в РФ | ~1 ₽/GB/мес |
| CDN | **Yandex Cloud CDN** | Для фото товаров | ~0.3 ₽/GB |
| Email | **SMTP от SendPulse / UniSender** | Российские аналоги SendGrid | от 0 ₽ |
| Мониторинг | **Sentry self-hosted** или sentry.io | + UptimeRobot (бесплатно) | 0–1 500 ₽ |
| CI/CD | **GitHub Actions** | auto-deploy при push в main | бесплатно |
| SSL | Let's Encrypt (certbot) | HTTPS обязателен | бесплатно |

> **Почему Timeweb:** надёжный российский провайдер, нет санкционных рисков, поддержка на русском, управляемые сервисы PostgreSQL и Redis снижают операционную нагрузку на одного разработчика.

> **MVP-деплой:** на старте — один VPS (Timeweb Cloud, 4 vCPU / 8 GB), все сервисы через docker-compose. При росте до 10+ тенантов — переход на Managed PostgreSQL и Redis.

### 16.2 docker-compose (prod)

```yaml
services:
  django:
    build: .
    command: gunicorn config.wsgi:application -w 4 -b 0.0.0.0:8000
    env_file: .env
    depends_on: [db, redis]

  celery_worker:
    build: .
    command: celery -A config worker -Q sync_import,avito_publish,avito_update,avito_price,avito_delete,ai_generate,notifications,billing --concurrency=4
    env_file: .env

  celery_beat:
    build: .
    command: celery -A config beat -S django_celery_beat.schedulers:DatabaseScheduler
    env_file: .env

  db:
    image: postgres:16
    volumes: [postgres_data:/var/lib/postgresql/data]

  redis:
    image: redis:7-alpine
    command: redis-server --maxmemory 512mb --maxmemory-policy allkeys-lru

  nginx:
    image: nginx:alpine
    volumes: [./nginx.conf:/etc/nginx/conf.d/default.conf]
    ports: ["80:80", "443:443"]
```

### 16.3 CI/CD (GitHub Actions)

```yaml
# .github/workflows/deploy.yml
on:
  push:
    branches: [main]
jobs:
  deploy:
    steps:
      - run: pytest --cov=apps --cov-fail-under=80
      - run: docker build + push to registry
      - run: ssh prod "docker-compose pull && docker-compose up -d"
      - run: ssh prod "docker-compose exec django python manage.py migrate"
```

---

## 17. НЕФУНКЦИОНАЛЬНЫЕ ТРЕБОВАНИЯ

| Требование | Целевое значение |
|-----------|----------------|
| Обработка одного изменения (1С → Avito) | ≤ 15 мин |
| Пропускная способность публикаций | ≥ 50 объявлений/час на тенант |
| Генерация описания (AI) | ≤ 8 сек |
| Загрузка таблицы 50К строк (Next.js) | ≤ 2 сек (виртуализация) |
| Idempotency всех Celery-задач | 100% (перезапуск без дублей) |
| Бэкап PostgreSQL | Ежедневно → S3, хранить 30 дней |
| Тест восстановления из бэкапа | Еженедельно (автоматически) |
| Uptime | ≥ 99.5% |

---

## 18. РОАДМАП (один fullstack-разработчик)

### Phase 1 — MVP, один клиент (45 дней)

| Этап | Задачи | Дней |
|------|-------|------|
| **0. Инфра** | Docker, PostgreSQL, Redis, GitHub Actions, домен, SSL | 3 |
| **1. Фундамент** | Tenant/User/Plan модели, мультитенантный middleware, базовая auth | 5 |
| **2. Источники** | OneCHTTPAdapter + OneCXMLAdapter + CSVAdapter, импорт, хэш-сравнение | 8 |
| **3. Файлы** | Yandex Cloud S3, PhotoUploadPipeline, дедупликация | 3 |
| **4. AI-агент** | DescriptionAgent, валидация, кэширование, fallback GPT-4o | 5 |
| **5. Avito API** | Auth, все операции, rate limiting, обработка всех ошибок, идемпотентность | 10 |
| **6. Anti-ban** | Gradual ramp-up, velocity control, текстовые вариации | 3 |
| **7. Биллинг** | Plan модели, LimitChecker, YooKassa, grace period | 5 |
| **8. Admin UI** | django-unfold, кастомные actions, страница статистики | 3 |
| **Итого Phase 1** | | **45 дней** |

### Phase 2 — SaaS-готовность (30 дней)

| Задачи | Дней |
|-------|------|
| Next.js Dashboard (все страницы + онбординг wizard) | 14 |
| Public API + Webhook система | 5 |
| Analytics (CTR, просмотры, ROI из Avito Stats API) | 5 |
| Shadow ban detection | 3 |
| White-label (custom domain per tenant) | 3 |
| **Итого Phase 2** | **30 дней** |

### Phase 3 — Рост (35 дней)

| Задачи | Дней |
|-------|------|
| Auto.ru адаптер | 20 |
| Расширенная аналитика (дашборд ROI, A/B заголовков) | 8 |
| SSO (Google OAuth) | 4 |
| SLA-мониторинг + Grafana | 3 |
| **Итого Phase 3** | **35 дней** |

**Итого до полноценного SaaS: ~110 дней (5.5 месяцев)**

---

## 19. КРИТЕРИИ ПРИЁМКИ

### 19.1 Функциональные (Phase 1)

| # | Сценарий | Ожидание |
|---|---------|---------|
| AT-01 | Включить флаг выгрузки в 1С для нового товара | Объявление на Avito ≤ 30 мин |
| AT-02 | Изменить цену в 1С | Цена обновлена на Avito ≤ 15 мин |
| AT-03 | Обнулить остаток в 1С | Объявление снято ≤ 15 мин |
| AT-04 | Загрузить 1000 строк CSV | Все товары в каталоге, ошибок < 1% |
| AT-05 | Достичь лимита плана | Новые публикации заблокированы, существующие активны |
| AT-06 | Avito API недоступна 1 час | Задачи накопились, выполнились после восстановления |
| AT-07 | Два тенанта одновременно | Полная изоляция данных, счётчики независимы |
| AT-08 | 50К SKU, все с флагом выгрузки | Публикация завершена ≤ 24 часа при gradual ramp-up |
| AT-09 | Отклонение модерацией Avito | Telegram-уведомление с причиной ≤ 1 мин |
| AT-10 | Просроченная подписка | Новые публикации блокируются на 8-й день, уведомление на email |

### 19.2 AI-агент

| # | Сценарий | Критерий |
|---|---------|---------|
| AI-01 | Товар с полными данными | confidence ≥ 0.9, title 50–100 симв. |
| AI-02 | Только наименование и бренд | confidence < 0.7, без выдумок |
| AI-03 | Запрещённые слова в ответе | Автоматический retry, логирование |
| AI-04 | Claude API недоступен | Fallback на GPT-4o, без падения |
| AI-05 | Превышен лимит AI-кредитов | Листинг в `requires_review`, уведомление |

---

## ПРИЛОЖЕНИЕ А — Переменные окружения (.env)

```bash
# Django
SECRET_KEY=...
DEBUG=False
ALLOWED_HOSTS=map.yourdomain.ru

# PostgreSQL
DATABASE_URL=postgresql://user:pass@db:5432/map_db

# Redis
REDIS_URL=redis://redis:6379/0

# Yandex Cloud S3
YC_S3_BUCKET=map-media-prod
YC_S3_ACCESS_KEY=...
YC_S3_SECRET_KEY=...
YC_CDN_DOMAIN=cdn.map.yourdomain.ru

# AI
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# Billing
YOOKASSA_SHOP_ID=...
YOOKASSA_SECRET_KEY=...

# Notifications
TELEGRAM_BOT_TOKEN=...
SENDPULSE_SMTP_LOGIN=...
SENDPULSE_SMTP_PASSWORD=...
DEFAULT_FROM_EMAIL=noreply@map.yourdomain.ru

# Security
FIELD_ENCRYPTION_KEY=...    # Fernet key для шифрования credentials
WEBHOOK_SIGNING_SECRET=...  # HMAC для подписи вебхуков

# Monitoring
SENTRY_DSN=...
```

---

## ПРИЛОЖЕНИЕ Б — Структура проекта

```
map/
├── config/
│   ├── settings/
│   │   ├── base.py
│   │   ├── development.py
│   │   └── production.py
│   ├── urls.py
│   └── celery.py
├── apps/
│   ├── tenants/          # Tenant, TenantUser, APIKey, TenantMiddleware
│   ├── billing/          # Plan, Subscription, Invoice, YooKassa webhook
│   ├── datasources/      # DataSourceConnection, все адаптеры (1C/XML/CSV)
│   ├── products/         # Product, ProductImage, PhotoUploadPipeline
│   ├── marketplaces/     # MarketplaceAccount, CategoryMapping, Listing
│   │   └── avito/        # AvitoAdapter, AvitoAuthManager, AvitoRateLimiter
│   ├── ai_agent/         # DescriptionAgent, prompts, валидация
│   ├── anti_ban/         # VelocityController, GradualRampUp, ShadowBanDetector
│   ├── sync/             # SyncOrchestrator, Celery задачи, SyncLog
│   ├── notifications/    # Telegram + Email шаблоны
│   ├── api/              # DRF views, serializers, webhook endpoints
│   └── analytics/        # Avito Stats, ROI (Phase 2)
├── docker-compose.yml
├── Dockerfile
├── nginx.conf
└── requirements.txt
```