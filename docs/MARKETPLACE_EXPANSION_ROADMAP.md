# Roadmap разделения маркетплейсов и подключения Ozon

Обновлено: 2026-08-29.

## Решение

P7 Avito не блокирует развитие продукта: он относится к retention и удалению
старых служебных данных. Следующая продуктовая цель — подготовить MAP к двум
маркетплейсам и только затем подключать Ozon к уже разделённому интерфейсу.

Ozon нельзя добавлять набором условных `if marketplace == ...` поверх экранов,
названий и статусов Avito. Сначала выделяется provider-neutral слой, при этом
текущий Avito runtime и его feed-флаги не меняются.

## M0 — инвентаризация контрактов

Результат: точная карта Avito-specific предположений без изменения runtime.

- маршруты и компоненты Dashboard;
- модели аккаунтов, листингов, статусов, логов и аналитики;
- API/OpenAPI поля, которые имеют Avito-specific имя или смысл;
- тарифные ограничения: общий лимит тенанта и лимит конкретного provider;
- уведомления, webhooks и audit events;
- category/attribute mapping;
- публикация, архивирование, статистика и provider URLs.

Gate: документированная матрица `общий контракт / Avito / Ozon`, список файлов
первого implementation package и отсутствие runtime-изменений.

## M1 — provider-neutral интерфейс

Результат: пользователь всегда понимает, с каким маркетплейсом и аккаунтом он
работает, даже до включения Ozon.

- общий переключатель/фильтр маркетплейса и аккаунта;
- `Листинги`: badge маркетплейса, provider/account filters, отдельные
  provider-specific статусы и действия;
- `Логи`: marketplace, account, operation и provider result как отдельные
  фильтруемые поля;
- `Аналитика`: общая сводка и отдельные вкладки по marketplace/account;
- `Настройки`: карточки подключений Avito и Ozon вместо Avito-only формы;
- нейтральные базовые тексты; особенности Avito остаются внутри Avito-панелей;
- URL и сохранённые фильтры должны позволять открыть конкретный marketplace.

Gate: frontend unit/typecheck/ESLint/build, API contract tests, tenant isolation,
Avito regression suite. Ozon I/O в этом этапе отсутствует.

## O1 — безопасное подключение Ozon Seller API

Результат: тенант может добавить Ozon-кабинет, проверить credentials и увидеть
понятное состояние подключения.

- отдельный provider adapter и credential schema;
- tenant-scoped account identity и защита повторного подключения;
- egress allowlist, deadlines, rate limits и bounded responses;
- recoverable onboarding без хранения секретов в логах;
- health/status polling и tenant-facing диагностика;
- fake-provider contract tests до любого production canary.

Gate: один выключенный release, fake API acceptance и отдельное разрешение на
реальный canary одного Ozon-кабинета.

## O2 — каталог, категории и обязательные атрибуты

Результат: товар можно подготовить к Ozon без влияния на Avito-карточку.

- Ozon category/attribute dictionaries и их versioned sync;
- отдельный mapping товара в Ozon offer;
- field-level preflight с разделением ошибок и рекомендаций;
- изображения, barcode, dimensions, VAT и warehouse requirements;
- provider-specific drawer sections без смешения полей Avito/Ozon.

Gate: schema drift tests, representative category fixtures, fail-open только
для необязательных рекомендаций и fail-closed для обязательных provider fields.

## O3 — публикация и reconciliation

Результат: create/update/archive Ozon offer с устойчивым локальным статусом.

- durable intent и idempotency для provider mutations;
- account-scoped очередь и rate limiting;
- неизвестный результат не повторяется вслепую;
- polling/reconciliation фактического provider status;
- tenant-visible ошибки, повторная проверка и безопасный retry;
- audit evidence без секретов и чужих tenant identifiers.

Gate: fault tests для timeout/429/5xx/partial result, полный backend/frontend
gate, выключенный deploy и один account-scoped production canary.

## O4 — цены, остатки, статистика и эксплуатация

Результат: Ozon работает как полноценный второй marketplace.

- обновление цены и остатков;
- статистика и аналитика с явным источником данных;
- уведомления и webhooks;
- observability, queue lag, dead-man и rate-limit alerts;
- runbook подключения, rollback и incident response;
- последовательный rollout: один аккаунт → несколько аккаунтов одного тенанта
  → новый тенант → fleet-default.

## Что не входит

- P7 Avito, GC и удаление XML;
- удаление legacy Avito-кода;
- proration Billing;
- заказы/FBO/FBS и финансовая сверка Ozon, пока они отдельно не активированы;
- автоматический перенос объявлений между маркетплейсами без подтверждения
  тенанта.
