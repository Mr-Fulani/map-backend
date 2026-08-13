# MAP — Marketplace Automation Platform

**Мультитенантная B2B SaaS-платформа для автоматизации работы с маркетплейсами в нише автозапчастей.**

Вместо того чтобы вручную обновлять сотни объявлений на Avito — MAP синхронизирует каталог автоматически. Подключил источник данных (1С, CSV) — и платформа сама создаёт, обновляет и архивирует объявления, следит за ценами и остатками, генерирует описания через AI и защищает аккаунты от блокировок.

---

## Какую боль решает

| Проблема | Как MAP решает |
|----------|----------------|
| Обновление 10 000+ объявлений вручную занимает часы | Синхронизация цен и остатков ≤ 15 минут |
| Новый товар появляется в 1С, но неделями не попадает на Avito | Публикация нового товара ≤ 30 минут |
| Написать продающее описание для каждого SKU невозможно | AI-агент генерирует описание по характеристикам из каталога |
| Avito блокирует аккаунты за слишком быструю публикацию | Anti-ban: gradual ramp-up + velocity control |
| Нет понимания, что происходит с объявлениями | Dashboard с KPI, логами, аналитикой CTR |
| Несколько Avito-аккаунтов — хаос | Изолированные аккаунты внутри одного тенанта |

---

## Для кого

| Сегмент | Характеристика | Типичный объём SKU |
|---------|---------------|-------------------|
| Авторазборщики | б/у запчасти, нет 1С, загрузка через CSV | 1К–20К |
| Оптовые дилеры | новые запчасти, 1С УТ 11.5, большой каталог | 10К–100К |
| Агрегаторы | несколько складов, несколько Avito-аккаунтов | 50К–500К |

---

## Что умеет платформа

### Синхронизация данных
- **Три источника данных:** 1С УТ через HTTP (CommerceML), 1С через XML-выгрузку, CSV-файлы
- Инкрементальные обновления — только изменившиеся позиции
- Шифрование credentials источников данных (Fernet)
- История синхронизаций с детальными логами по каждому событию

### Публикация на Avito
- Полный CRUD листингов: создание, обновление цены/остатка, архивирование
- Поддержка нескольких Avito-аккаунтов на одного тенанта
- Автоматическое маппинг категорий каталога → категории Avito
- Retry-логика: до 3 попыток при временных ошибках API Avito
- Обработка кодов отклонения и сохранение причины отказа

### Anti-ban защита
- **Gradual ramp-up:** новый аккаунт начинает с 100 объявлений в день, выходит на полную мощность за 30 дней
- **Velocity control:** лимиты на публикацию (50/час) и обновления (200/час) через Redis-счётчики
- **Shadow ban detector:** мониторинг CTR, предупреждение при аномально низких показателях

### AI-агент
- Генерация описаний через настраиваемые OpenAI, Anthropic и OpenAI-compatible модели
- Валидация готовых описаний перед отправкой на Avito
- Подсчёт использованных AI-кредитов по тарифному плану
- Промпты адаптированы для ниши автозапчастей

### Биллинг
- Четыре тарифных плана с разными лимитами (Starter → Enterprise)
- 14-дневный бесплатный Trial (план Business, без ввода карты)
- Интеграция с ЮKassa: оплата, вебхуки, история счетов
- Скидка 20% при оплате за год
- Grace period 7 дней при просроченной оплате
- Автоматическая блокировка новых публикаций при превышении лимитов

Платёжный код реализован, но production checkout включается только явным
`BILLING_ENABLED=true` после настройки credentials и webhook YooKassa. Без этого
флага тарифы, текущая подписка, лимиты и история остаются доступны только для чтения.

### Уведомления
- Telegram-уведомления об ошибках публикации и критических событиях
- Email-уведомления о платёжных событиях
- Настройка порогов уведомлений в личном кабинете

Текущий email-канал отправляет только транзакционные письма платформы с
проверенного platform-домена. Письма от бренда конкретного tenant-а — будущий
отдельный контур с verified sender identity, quotas, audit и domain-scoped/BYOK key.

### Dashboard (Next.js)
- Главная страница с KPI: активные листинги, синхронизации, ошибки, AI-кредиты
- Каталог товаров с поиском (артикул, название, бренд) и фильтрами
- Страница листингов с фильтрацией по статусу (активные, черновики, отклонены...)
- Логи синхронизаций с фильтрацией по статусу и дате
- Аналитика: CTR, просмотры, избранное, контакты из Avito Stats API
- Биллинг: текущий план, смена тарифа, история платежей
- Настройки: профиль организации, Avito-аккаунты, API-ключи, уведомления
- Онбординг-визард: подключение Avito и источника данных с нуля

### Многотенантность
- Полная изоляция данных: каждый тенант видит только своё
- Два способа аутентификации: JWT (для Dashboard) и API Key (для прямых интеграций)
- Ролевая модель: Owner, Admin, Operator, Viewer
- REST API + Swagger-документация для внешних интеграций
- Вебхуки на события синхронизации

---

## Тарифные планы

| План | Активных объявлений | SKU в каталоге | AI-кредитов/мес | Цена |
|------|-------------------|----------------|-----------------|------|
| **Starter** | до 1 000 | до 5 000 | 1 000 | 4 900 ₽/мес |
| **Business** | до 10 000 | до 30 000 | 5 000 | 14 900 ₽/мес |
| **Pro** | до 50 000 | до 150 000 | 20 000 | 34 900 ₽/мес |
| **Enterprise** | без лимита | без лимита | 50 000 | от 79 900 ₽/мес |

Trial: 14 дней бесплатно на плане Business. Скидка 20% при оплате за год.

---

## Технический стек

### Backend
- **Django 5** + Django REST Framework
- **PostgreSQL 16** — основная БД, изоляция данных по tenant FK
- **Celery** + **Redis 7** — фоновые задачи и очереди
- **django-celery-beat** — расписание: синхронизация каждые 5 мин, обновление статистики каждый час
- **django-unfold** — кастомизированная Django Admin с тёмной темой
- **drf-spectacular** — автогенерация OpenAPI/Swagger
- **Yandex Cloud S3** — хранение изображений товаров
- **Sentry SDK** — опциональный мониторинг ошибок при настроенном `SENTRY_DSN`
- **OpenAI / Anthropic / Gemini / DeepSeek / Kimi** — маршрутизируемая AI-генерация

### Frontend
- **Next.js 16.3** + **React 19.2** (App Router, TypeScript)
- **Tailwind CSS** + **shadcn/ui** — компонентная библиотека
- **Axios** — HTTP-клиент с интерсепторами для JWT refresh
- **Sonner** — toast-уведомления

### Инфраструктура
- **Docker Compose**: Django, PostgreSQL, отдельные cache/broker Redis,
  Celery workers/Beat, Next.js, Nginx, ограничивающий egress proxy и backup job
- **Nginx** — reverse proxy, rate limiting
- **GitHub Actions** — CI (backend/frontend тесты, OpenAPI, dependency/OCI
  vulnerability gates, SBOM) и gated deploy workflow; автоматический production
  deploy требует отдельно настроенных GitHub environment, variable и secrets
- **Hetzner Cloud** — текущий production host

---

## Быстрый старт

### Требования
- Docker + Docker Compose plugin
- Python 3.12.13 (версия CI/runtime; 3.12.x для локальных no-Docker проверок)
- Node.js 24.18.0 и npm 12.0.2 (версии CI/runtime)

### Режим A: весь runtime в Docker Compose

```bash
# 1. Клонировать репозиторий
git clone https://github.com/OWNER/REPOSITORY.git
cd saas_poster

# 2. Создать .env из примера
cp .env.example .env
# Заполнить переменные (см. раздел "Переменные окружения")

# 3. Первый bootstrap: поднять только зависимости, выполнить миграции/seed/Beat
#    one-shot командами и лишь затем запустить все сервисы, включая frontend
make bootstrap

# 4. Создать суперпользователя для Django Admin
make superuser
```

Замените `OWNER/REPOSITORY` на фактический путь репозитория перед клонированием.

После запуска:
- Django Admin: `http://localhost:8000/admin/`
- API Swagger: `http://localhost:8000/api/docs/`
- Frontend: `http://localhost:3000/`

### Режим B: backend в Compose, Next.js на хосте

Это взаимоисключающий с режимом A вариант: не запускайте одновременно
containerized frontend через `make up` и локальный Next.js на том же порту.
`dev.sh` фиксирует Compose file/project, проверяет все публикуемые порты, применяет
миграции до старта Django и при `Ctrl+C` останавливает только этот проект.

```bash
cd frontend
npm ci --strict-allow-scripts
cd ..
./dev.sh
```

---

## Переменные окружения

Полный актуальный перечень находится в [`.env.example`](.env.example). Ключевые
имена: `DJANGO_SECRET_KEY`, `DATABASE_URL`, `CACHE_REDIS_URL`,
`CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `COORDINATION_REDIS_URL`,
`FIELD_ENCRYPTION_KEYS`, `YC_S3_*`, ключи AI-провайдеров, `AVITO_*`,
`YOOKASSA_*`, `RESEND_API_KEY`, `DEFAULT_FROM_EMAIL`,
`EMAIL_HTTP_PROXY_URL` и `SENTRY_DSN`.
`REDIS_URL` используется только как fallback локальной разработки.

Для production обязательны отдельные случайные PostgreSQL/cache-Redis/
broker-Redis/Fernet secrets.
Порядок ротации, webhook delivery, retention и egress policy описаны в
[`docs/PRODUCTION_SECURITY.md`](docs/PRODUCTION_SECURITY.md).

Для локального frontend используйте `NEXT_PUBLIC_API_URL=http://localhost:8000`.
В production оставьте `NEXT_PUBLIC_API_URL` пустым: браузер будет обращаться к
same-origin `/api`, который Nginx проксирует в Django.

---

## Архитектура

```
Клиент A (1С УТ)      Клиент B (CSV)        Клиент C (1С + XML)
        │                    │                       │
        └────────────────────┼───────────────────────┘
                             ▼
              ┌──────────────────────────────┐
              │      MAP Django Backend      │
              │                              │
              │   DataSource Adapters        │
              │   ├── OneCHTTPAdapter        │
              │   ├── OneCXMLAdapter         │
              │   └── CSVAdapter             │
              │                              │
              │   AI Agent (multi-provider)  │
              │   Anti-ban System            │
              │   Billing (ЮKassa)           │
              │   Notifications              │
              │                              │
              │   Marketplace Adapters       │
              │   └── AvitoAdapter    ← MVP  │
              └──────────────────────────────┘
                             │
                    ┌────────┴────────┐
                    ▼                 ▼
              Avito API          Next.js Dashboard
              (публикация)       (SaaS UI)
```

**Поток синхронизации:**
1. Celery Beat каждые 5 минут запускает `sync_all_tenants`
2. Для каждого активного тенанта: DataSource Adapter тянет изменения из источника
3. Изменения сохраняются в `Product` (цена, остаток, описание)
4. Если товар новый → AI-агент генерирует описание
5. `publish_listing_task` публикует/обновляет объявление через Avito API
6. Результат пишется в `SyncLog`, метрики обновляются
7. При ошибках → `retry` + уведомление в Telegram

---

## API

Базовый URL: `/api/v1/`

Аутентификация: JWT или API Key в заголовке `Authorization: Bearer <token>`.
API-ключ имеет префикс `map_sk_`.

### Основные эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| `POST` | `/auth/register/` | Регистрация тенанта |
| `POST` | `/auth/token/` | Получить JWT-токен |
| `GET` | `/products/` | Каталог товаров (поиск, фильтры, пагинация) |
| `GET/PATCH` | `/products/{id}/` | Карточка товара, включение/выключение выгрузки |
| `GET` | `/listings/` | Листинги (фильтр по статусу) |
| `GET` | `/logs/` | Логи синхронизаций |
| `GET` | `/billing/plans/` | Тарифные планы |
| `GET` | `/billing/subscription/` | Текущая подписка тенанта |
| `POST` | `/billing/checkout/` | Создать платёж (ЮKassa; только при включённом billing) |
| `GET` | `/billing/invoices/` | История платежей |
| `GET` | `/analytics/` | KPI: CTR, просмотры, конверсия |
| `GET/POST` | `/tenant/api-keys/` | Управление API-ключами |
| `GET/POST` | `/webhooks/` | Настройка webhook endpoint-ов |
| `GET` | `/webhooks/deliveries/` | Аудит и статусы webhook-доставок |

Полная документация: `/api/docs/` (Swagger UI)

---

## Структура проекта

```
saas_poster/
├── apps/
│   ├── ai_agent/        — Claude API, промпты, валидация
│   ├── analytics/       — Avito Stats API, метрики CTR
│   ├── anti_ban/        — ramp-up, velocity, shadow ban
│   ├── billing/         — планы, подписки, ЮKassa
│   ├── core/            — TimestampedModel, middleware, утилиты
│   ├── datasources/     — адаптеры 1С/CSV, шифрование
│   ├── marketplaces/    — Avito-аккаунты, листинги, адаптер Avito
│   ├── image_search/    — поиск и оценка изображений
│   ├── media_processing/— обработка изображений
│   ├── web_research/    — товарные и ценовые интернет-исследования
│   ├── notifications/   — Telegram + Email уведомления
│   ├── products/        — каталог товаров и изображений
│   ├── sync/            — SyncLog, задачи синхронизации
│   ├── tenants/         — Tenant, TenantUser, APIKey, JWT
│   └── users/           — кастомный User (email-based auth)
├── config/
│   ├── settings/        — base, development, production
│   ├── celery.py
│   └── urls.py
├── frontend/            — Next.js 16.3 / React 19 Dashboard
├── requirements/
│   ├── base.in          — общие прямые Python-зависимости
│   ├── dev.in           — инструменты разработки и тестов
│   ├── prod.in          — production WSGI/runtime слой
│   ├── ci-tools.in      — изолированные инструменты supply-chain CI
│   └── *.txt            — воспроизводимые hash-locked lock-файлы
├── docker-compose.yml   — локальная разработка
├── docker-compose.prod.yml
├── docker-compose.restore.yml
├── Makefile
└── ROADMAP_MAP.md
```

---

## Статус разработки

### ✅ Реализовано (Phase 1 + Phase 2)

- [x] Мультитенантная архитектура: изоляция данных, middleware, ролевая модель
- [x] Авторизация: JWT + API Key, refresh токены
- [x] Биллинг: 4 тарифа, Trial, ЮKassa checkout + вебхуки, история счетов
- [x] Источники данных: 1С HTTP, 1С XML, CSV (с шифрованием credentials)
- [x] Каталог товаров: импорт, хранение, фильтрация, изображения на S3
- [x] Avito-адаптер: публикация, обновление, архивирование, retry
- [x] AI-агент: multi-provider routing, генерация описаний, кошелёк и кредиты
- [x] Anti-ban: gradual ramp-up, velocity control, shadow ban detector
- [x] Уведомления: Telegram + Email, настройки per-tenant
- [x] Логи синхронизаций: SyncLog, фильтрация, автоочистка > 90 дней
- [x] Django Admin: Unfold-тема, русский язык, кастомная страница статистики
- [x] Celery Beat: расписание всех фоновых задач
- [x] Next.js Dashboard: KPI, каталог, листинги, логи, аналитика, биллинг, настройки
- [x] REST API + OpenAPI/Swagger документация
- [x] Управление webhook endpoint-ами и безопасная тестовая отправка
- [x] Transactional webhook outbox, HMAC-подпись, retry и аудит доставок
- [x] Soft-delete и автоматическая retention-очистка критичных сущностей
- [x] CI и deploy-контракт: GitHub Actions (lint + тесты + OpenAPI без
      предупреждений + gated workflow для проверенного commit SHA)
- [x] Backup/restore tooling: зашифрованный pre-migration backup, проверка
      подписи/VersionId/freshness, lifecycle policy и изолированный restore drill

### ⚠️ Требует production-настройки и регулярной проверки

- [ ] Настроить GitHub environment `production`, deploy variable/secrets и
      отдельный ограниченный deploy account; до этого workflow будет пропущен.
- [ ] Включить ежедневный backup timer и hourly freshness timer, применить S3
      lifecycle и ежемесячно фиксировать успешный restore drill/RTO.
- [ ] Настроить `SENTRY_DSN`, внешний uptime/dead-man monitoring и алерты на 5xx,
      очередь, workers, диск и backup freshness.

### 🚧 В планах (Phase 3)

- [ ] Auto.ru адаптер (параллельная публикация на Auto.ru)
- [ ] Мультиплатформенный листинг (один товар → Avito + Auto.ru)
- [ ] Расширенная аналитика: A/B тест заголовков, тепловые карты
- [ ] White-label: кастомный домен и брендинг для Enterprise
- [ ] Производственная нагрузка: нагрузочное тестирование 50К SKU

---

## Разработка

```bash
# Запустить тесты
make test

# Линтер
make lint
# или
flake8 .

# Создать миграции
make migrations

# Django shell
make shell

# Проверки без обращения к Docker daemon
make runtime-check
make frontend-test
cd frontend && npm run typecheck && npm run lint && npm run build
```

### Команды Makefile

| Команда | Описание |
|---------|----------|
| `make bootstrap` | Безопасно подготовить пустую БД и запустить первый dev runtime |
| `make up` | Поднять все сервисы |
| `make down` | Остановить все сервисы |
| `make shell` | Django shell |
| `make migrate` | Применить миграции |
| `make test` | Запустить тесты с coverage |
| `make lint` | Проверить весь Python-код через flake8 |
| `make typecheck-backend` | Проверить расширяемый type-clean baseline из `mypy.ini` |
| `make runtime-check` | Проверить Compose/deploy/health contracts без Docker daemon |
| `make frontend-test` | Запустить критичные frontend unit/contract тесты без Docker |
| `make backup` | Создать зашифрованный production backup (ops profile) |
| `make backup-check` | Проверить свежесть последнего production backup |

Production runbooks: [deployment](docs/DEPLOYMENT.md),
[security](docs/PRODUCTION_SECURITY.md), [backup/restore](docs/BACKUP_RESTORE.md)
и [release checklist](docs/RELEASE_CHECKLIST.md).

---

## Лицензия

Proprietary. Все права защищены.
