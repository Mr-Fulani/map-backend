# MAP — Marketplace Automation Platform

**Мультитенантная B2B SaaS-платформа для работы с Avito и Ozon. Основной проверенный сценарий — автозапчасти.**

MAP объединяет импорт каталога из 1С/CSV, обогащение, поиск и модерацию фото,
подготовку карточек и публикацию в выбранные кабинеты маркетплейсов. Общие данные
готовятся у товара; категории, цены и требования площадки проверяются у листинга.
Автоматизация включается отдельно для конкретного подключения, не самим фактом
импорта товара. Ограничение запросов снижает риск ошибок, но не гарантирует
отсутствие блокировок со стороны площадки.

Актуализация **2026-09-06**: [состояние Ozon](docs/OZON_STATUS.md),
[Avito feed](docs/AVITO_FEED_STATUS.md),
[обработка фото и короткие видео — аудит незавершённой функции](docs/media_processing.md).

---

## Какую боль решает

| Проблема | Как MAP решает |
|----------|----------------|
| Обновление большого каталога вручную занимает часы | Фоновая синхронизация в подключениях с включённой автоматизацией |
| Карточку приходится готовить для каждой площадки с нуля | Общие факты и медиа → модерация → отдельная проверка и публикация в выбранный кабинет |
| Написать продающее описание для каждого SKU невозможно | AI-агент генерирует описание по характеристикам из каталога |
| Площадка ограничивает частоту запросов | Ограничения и повторные попытки в соответствующем adapter/operation workflow |
| Нет понимания, что происходит с объявлениями | Dashboard, статусы и логи по кабинетам; CTR относится к Avito, не ко всем площадкам |
| Несколько кабинетов Avito и Ozon — хаос | Изолированные аккаунты внутри одного тенанта |

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

### Публикация на Ozon

- Несколько кабинетов на тенанта, отдельные credentials, настройки и операции
- Собственное дерево категорий/типов, включение веток, наценки с наследованием
- Подготовка атрибутов, справочники и проверка готовности в drawer выбранного кабинета
- Публикация, сверка модерации/состояния, снятие с продажи, работа с ценой и остатком
- Общие логи и диагностика синхронизации; FBS-заказы — отдельная опциональная функция
- Автоматизация цен/остатков и заказов не включается вместе с подключением ключа

Точные границы и реально включённые флаги AlfaPro — в
[Ozon status](docs/OZON_STATUS.md); аналитика продаж и дальнейший rollout остаются
отдельными этапами.

### Медиа товаров

- Поиск, загрузка и модерация обычных фото используют общий товарный сценарий
- Проверки фото перед отправкой не равны обработке или генерации изображений
- `media_processing` пока содержит backend foundation без production-адаптера
  и завершённого редактора; короткие видео существуют только в плане

Что уже сделано, какие есть риски для обеих площадок и как завершать функцию —
[аудит медиа](docs/media_processing.md).

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
- Диагностика Ozon: устойчивый сбой/задержка, срок ключа, подтверждённое восстановление
- Email-уведомления о платёжных событиях
- Настройка порогов уведомлений в личном кабинете

Текущий email-канал отправляет только транзакционные письма платформы с
проверенного platform-домена. Письма от бренда конкретного tenant-а — будущий
отдельный контур с verified sender identity, quotas, audit и domain-scoped/BYOK key.

### Dashboard (Next.js)
- Главная страница с KPI: активные листинги, синхронизации, ошибки, AI-кредиты
- Каталог товаров с поиском (артикул, название, бренд) и фильтрами
- Листинги с выбором маркетплейса/кабинета, отдельными полями и проверками готовности
- Логи синхронизаций с фильтрацией по маркетплейсу, кабинету, статусу и дате
- Аналитика: CTR, просмотры, избранное, контакты из Avito Stats API
- Биллинг: текущий план, смена тарифа, история платежей
- Настройки: организация, кабинеты Avito/Ozon, категории и наценки площадок, ключи, уведомления
- Онбординг-визард: подключение Avito и источника данных с нуля

### Многотенантность
- Полная изоляция данных: каждый тенант видит только своё
- Два способа аутентификации: JWT (для Dashboard) и API Key (для прямых интеграций)
- Ролевая модель: Owner, Admin, Operator, Viewer
- REST API + Swagger-документация для внешних интеграций
- Вебхуки на события синхронизации

---

## Тарифные планы

| План | Активных объявлений всего в MAP | SKU в каталоге | AI-кредитов/мес | Цена |
|------|----------------------------------|----------------|-----------------|------|
| **Starter** | до 1 000 | до 5 000 | 1 000 | 4 900 ₽/мес |
| **Business** | до 10 000 | до 30 000 | 5 000 | 14 900 ₽/мес |
| **Pro** | до 50 000 | до 150 000 | 20 000 | 34 900 ₽/мес |
| **Enterprise** | без лимита | без лимита | 50 000 | от 79 900 ₽/мес |

Trial: 14 дней бесплатно на плане Business. Скидка 20% при оплате за год.

Лимит тарифа считается суммарно по тенанту. Один Avito-аккаунт содержит не
более 10 000 активных объявлений; больший общий объём Pro и Enterprise
распределяется между несколькими подключёнными аккаунтами.

### Текущий режим фидов Avito

P0–P6 внедрены. Production использует durable/private цепочку для готовых Avito
Autoload аккаунтов. Новый успешно подключённый аккаунт автоматически получает
managed stable endpoint; ручной allowlist не нужен. XML хранится в закрытом
versioned bucket, а Avito получает короткоживущий redirect на точную версию.

Текущий production-контракт:

```text
AVITO_STATUS_LIFECYCLE_MODE=dual_write
MARKETPLACE_FEED_RUN_MODE=durable
MARKETPLACE_FEED_INGRESS_MODE=dual_write
MARKETPLACE_FEED_ARTIFACT_MODE=active
MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS=
MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=false
MARKETPLACE_FEED_STORAGE_MODE=stable_bridge
```

Пустой cutover allowlist означает fleet-default. Profile migration остаётся
`false`, потому что массовый sweep старых профилей выключен; штатный onboarding
нового аккаунта при этом работает. P7 cleanup/GC/object deletion/`0039`
заморожен до отдельного решения. Исходный смешанный WIP сохранён только как
исторический `not-for-merge` snapshot и не должен целиком попадать в release.

Актуальные границы и порядок:

- [текущее состояние](docs/AVITO_FEED_STATUS.md);
- [roadmap P0–P7](docs/AVITO_FEED_ROADMAP.md);
- [карта разделения изменений](docs/AVITO_FEED_CHANGESET_MANIFEST.md);
- [обязательные правила выполнения](docs/ENGINEERING_EXECUTION_RULES.md);
- [актуальный технический долг](TECH_DEBT.md);
- [текущий статус Ozon](docs/OZON_STATUS.md);
- [история разделения маркетплейсов](docs/MARKETPLACE_EXPANSION_ROADMAP.md).

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
  vulnerability gates, SBOM; docs-only fast path, параллельные backend shards)
  и gated deploy workflow; автоматический production deploy требует отдельно
  настроенных GitHub environment, variable и secrets
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
              │   ├── AvitoAdapter           │
              │   └── OzonSellerClient       │
              └──────────────────────────────┘
                             │
                    ┌────────┴────────┐
                    ▼                 ▼
              Avito / Ozon       Next.js Dashboard
              (раздельные API)   (общий SaaS UI)
```

**Основной пользовательский поток:**

1. Источник обновляет общий `Product`: исходные данные, цену, остаток.
2. Тенант запускает обогащение и поиск фото, проверяет факты и медиа. Профильные
   автопарсеры не должны применяться к товарам других категорий, например одежде.
3. Подготовленные сведения используются в листингах выбранных кабинетов.
   В drawer проверяются и исправляются поля конкретной площадки.
4. Подтверждённая публикация идёт через соответствующий workflow Avito или Ozon;
   фоновые задачи сверяют результат площадки, а не считают постановку в очередь успехом.
5. Результаты видны в логах/диагностике. Синхронизация цен/остатков и заказов
   регулируется настройками подключения; импорт не является универсальной командой
   немедленной публикации во все аккаунты.

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
│   ├── ai_agent/        — multi-provider AI, промпты, валидация
│   ├── analytics/       — Avito Stats API, метрики CTR
│   ├── anti_ban/        — ramp-up, velocity, shadow ban
│   ├── billing/         — планы, подписки, ЮKassa
│   ├── core/            — TimestampedModel, middleware, утилиты
│   ├── datasources/     — адаптеры 1С/CSV, шифрование
│   ├── marketplaces/    — аккаунты Avito/Ozon, проекции, адаптеры и операции
│   ├── image_search/    — поиск и оценка изображений
│   ├── media_processing/— foundation обработки фото; адаптер/редактор не завершены
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

Актуально на **2026-09-06**; старые Phase-чеклисты не являются отчётом о production.

| Контур | Состояние и источник истины |
|---|---|
| Avito | Рабочий publication/feed контур; дальнейшие feed-изменения заморожены. [Статус](docs/AVITO_FEED_STATUS.md) |
| Ozon | Реализован и используется AlfaPro; включение записи не включает все виды автоматизации. [Статус и ограничения](docs/OZON_STATUS.md) |
| Общая работа тенанта | Каталог, обогащение и медиа товара → отдельные листинги выбранных кабинетов |
| Обработка/генерация фото | Незавершённый backend foundation, не готовая пользовательская функция. [Аудит](docs/media_processing.md) |
| Короткое видео | Не реализовано; отдельный будущий media pipeline |
| CI/deploy | Production-релиз `04bb573` прошёл полный CI и deploy. [Точные runs, команды и результаты](docs/OZON_HEALTH_RELEASE.md) |
| Наблюдаемость | Локальная диагностика Ozon выложена; внешний dead-man и test-fire нельзя считать доказанными только по наличию кода. [Runbook](docs/OBSERVABILITY.md) |

Backup/restore требует регулярного контроля по [runbook](docs/BACKUP_RESTORE.md),
независимо от успешной последней выкладки. Последний полный CI: **2995 passed,
2 skipped**, frontend **96 passed**, contracts **67 passed**; это baseline релиза,
не обещание повторного полного прогона при каждом docs-only изменении.

Оставшиеся подтверждённые риски: [TECH_DEBT.md](TECH_DEBT.md). Расширенная аналитика,
новые маркетплейсы, white-label и большие нагрузочные сценарии — отдельные
продуктовые пакеты; они не активируются обновлением документации.

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
