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
- Генерация продающих описаний по характеристикам товара (Claude API)
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

### Уведомления
- Telegram-уведомления об ошибках публикации и критических событиях
- Email-уведомления о платёжных событиях
- Настройка порогов уведомлений в личном кабинете

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

| План | Активных объявлений | SKU в каталоге | AI-генераций/мес | Цена |
|------|-------------------|----------------|-----------------|------|
| **Starter** | до 1 000 | до 5 000 | 1 000 | 4 900 ₽/мес |
| **Business** | до 10 000 | до 30 000 | 5 000 | 14 900 ₽/мес |
| **Pro** | до 50 000 | до 150 000 | 20 000 | 34 900 ₽/мес |
| **Enterprise** | без лимита | без лимита | без лимита | от 79 900 ₽/мес |

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
- **Yandex Cloud S3** — хранение изображений товаров и PDF-счетов
- **Sentry** — мониторинг ошибок
- **Claude API (Anthropic)** — AI-генерация описаний

### Frontend
- **Next.js 14** (App Router, TypeScript)
- **Tailwind CSS** + **shadcn/ui** — компонентная библиотека
- **Axios** — HTTP-клиент с интерсепторами для JWT refresh
- **Sonner** — toast-уведомления

### Инфраструктура
- **Docker** + **docker-compose** (django, postgres, redis, celery_worker, celery_beat)
- **Nginx** — reverse proxy, rate limiting
- **GitHub Actions** — CI (pytest, flake8) + CD (деплой при пуше в main)
- **Timeweb Cloud** — хостинг

---

## Быстрый старт

### Требования
- Docker + docker-compose
- Python 3.12+ (для локальной разработки без Docker)
- Node.js 20+ (для фронтенда)

### Запуск через Docker

```bash
# 1. Клонировать репозиторий
git clone <repo-url>
cd saas_poster

# 2. Создать .env из примера
cp .env.example .env
# Заполнить переменные (см. раздел "Переменные окружения")

# 3. Поднять все сервисы
docker compose up -d

# 4. Выполнить миграции и загрузить тарифные планы
docker compose exec django python manage.py migrate
docker compose exec django python manage.py loaddata billing_plans

# 5. Создать суперпользователя для Django Admin
docker compose exec django python manage.py createsuperuser
```

После запуска:
- Django Admin: `http://localhost:8000/admin/`
- API Swagger: `http://localhost:8000/api/docs/`
- Frontend: `http://localhost:3000/`

### Запуск фронтенда

```bash
cd frontend
npm install
npm run dev
```

---

## Переменные окружения

```env
# Django
SECRET_KEY=your-secret-key
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1

# База данных
DATABASE_URL=postgres://user:password@postgres:5432/map_db

# Redis
REDIS_URL=redis://redis:6379/0

# Yandex Cloud S3
YC_BUCKET_NAME=your-bucket
YC_ACCESS_KEY_ID=your-key
YC_SECRET_ACCESS_KEY=your-secret
YC_ENDPOINT_URL=https://storage.yandexcloud.net

# Claude API (AI-генерация описаний)
ANTHROPIC_API_KEY=sk-ant-...

# ЮKassa (биллинг)
YOOKASSA_SHOP_ID=your-shop-id
YOOKASSA_SECRET_KEY=your-secret-key

# Telegram-уведомления
TELEGRAM_BOT_TOKEN=your-bot-token

# Email (SendPulse SMTP)
EMAIL_HOST=smtp.sendpulse.com
EMAIL_PORT=587
EMAIL_HOST_USER=your-email
EMAIL_HOST_PASSWORD=your-password

# Sentry
SENTRY_DSN=https://...@sentry.io/...
```

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
              │   AI Agent (Claude API)      │
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
1. Celery Beat каждые 5 минут запускает `sync_all_tenants_task`
2. Для каждого активного тенанта: DataSource Adapter тянет изменения из источника
3. Изменения сохраняются в `Product` (цена, остаток, описание)
4. Если товар новый → AI-агент генерирует описание
5. `publish_listing_task` публикует/обновляет объявление через Avito API
6. Результат пишется в `SyncLog`, метрики обновляются
7. При ошибках → `retry` + уведомление в Telegram

---

## API

Базовый URL: `/api/v1/`

Аутентификация: Bearer JWT или API Key в заголовке `Authorization: Api-Key <key>`

### Основные эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| `POST` | `/auth/register/` | Регистрация тенанта |
| `POST` | `/auth/jwt/create/` | Получить JWT-токен |
| `GET` | `/products/` | Каталог товаров (поиск, фильтры, пагинация) |
| `GET/PATCH` | `/products/{id}/` | Карточка товара, включение/выключение выгрузки |
| `GET` | `/listings/` | Листинги (фильтр по статусу) |
| `GET` | `/logs/` | Логи синхронизаций |
| `GET` | `/billing/plans/` | Тарифные планы |
| `GET` | `/billing/subscription/` | Текущая подписка тенанта |
| `POST` | `/billing/checkout/` | Создать платёж (ЮKassa) |
| `GET` | `/billing/invoices/` | История платежей |
| `GET` | `/analytics/summary/` | KPI: CTR, просмотры, конверсия |
| `GET/POST` | `/tenants/api-keys/` | Управление API-ключами |
| `GET/POST` | `/tenants/webhooks/` | Настройка вебхуков |

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
│   ├── notifications/   — Telegram + Email уведомления
│   ├── products/        — каталог товаров и изображений
│   ├── sync/            — SyncLog, задачи синхронизации
│   ├── tenants/         — Tenant, TenantUser, APIKey, JWT
│   └── users/           — кастомный User (email-based auth)
├── config/
│   ├── settings/        — base, development, production
│   ├── celery.py
│   └── urls.py
├── frontend/            — Next.js 14 Dashboard
├── requirements/
│   ├── base.txt
│   ├── dev.txt
│   └── prod.txt
├── docker-compose.yml
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
- [x] AI-агент: генерация описаний через Claude, подсчёт кредитов
- [x] Anti-ban: gradual ramp-up, velocity control, shadow ban detector
- [x] Уведомления: Telegram + Email, настройки per-tenant
- [x] Логи синхронизаций: SyncLog, фильтрация, автоочистка > 90 дней
- [x] Django Admin: Unfold-тема, русский язык, кастомная страница статистики
- [x] Celery Beat: расписание всех фоновых задач
- [x] Next.js Dashboard: KPI, каталог, листинги, логи, аналитика, биллинг, настройки
- [x] REST API + OpenAPI/Swagger документация
- [x] Вебхуки на события синхронизации
- [x] CI/CD: GitHub Actions (lint + тесты + деплой)

### 🚧 В планах (Phase 3)

- [ ] Auto.ru адаптер (параллельная публикация на Auto.ru)
- [ ] Мультиплатформенный листинг (один товар → Avito + Auto.ru)
- [ ] Расширенная аналитика: A/B тест заголовков, тепловые карты
- [ ] White-label: кастомный домен и брендинг для Enterprise
- [ ] Производственная нагрузка: нагрузочное тестирование 50К SKU
- [ ] Backup/restore: автоматические бэкапы PostgreSQL в S3

---

## Разработка

```bash
# Запустить тесты
make test
# или
docker compose exec django pytest --cov=apps

# Линтер
make lint
# или
flake8 apps/

# Создать миграции
docker compose exec django python manage.py makemigrations

# Django shell
make shell
```

### Команды Makefile

| Команда | Описание |
|---------|----------|
| `make up` | Поднять все сервисы |
| `make down` | Остановить все сервисы |
| `make shell` | Django shell |
| `make migrate` | Применить миграции |
| `make test` | Запустить тесты с coverage |
| `make lint` | Проверить код (flake8) |

---

## Лицензия

Proprietary. Все права защищены.
