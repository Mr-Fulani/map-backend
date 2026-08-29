# Инвентаризация контрактов маркетплейсов: Avito и Ozon

Обновлено: 2026-08-29.

Статус: M0 `VERIFIED`, M1a `ENABLED`, M1b `ENABLED`.
Документ описывает состояние репозитория от commit
`fde9564f7ed183c30d852a4024a7957a667fee42` до реализации M1b 2026-08-29.

Главный roadmap: [MARKETPLACE_EXPANSION_ROADMAP.md](MARKETPLACE_EXPANSION_ROADMAP.md).
Рабочее состояние Avito: [AVITO_FEED_STATUS.md](AVITO_FEED_STATUS.md).

## Пользовательский результат M0

Зафиксирована точная граница между общим ядром MAP, работающим Avito и будущим
Ozon. Следующий пакет может разделить интерфейс и API без изменения цепочки
Avito и без преждевременных вызовов Ozon.

В M0 не входят:

- migrations и изменения моделей;
- новый provider adapter, очередь или periodic task;
- генерация Ozon API key и сохранение credentials;
- запросы от приложения в реальный кабинет AlfaPro;
- публикация, изменение цены или остатка реальной карточки;
- P7 Avito, retention, GC и удаление XML.

## Зафиксированные продуктовые решения

1. Один tenant может иметь несколько аккаунтов Avito и несколько аккаунтов
   Ozon. Любой listing, operation, status, log и metric всегда привязан к
   точному `tenant + marketplace + account`.
2. Первый Ozon MVP использует FBS и один выбранный склад на один Ozon account.
   Модель должна позволить добавить несколько складов позднее без смены
   идентичности offer. Обработка заказов, возвратов и FBO в MVP не входит.
3. Barcode, габариты, вес и НДС берутся из 1C, если значение присутствует и
   проходит валидацию. Иначе пользователь заполняет fallback в MAP. Payload
   обязан показывать и сохранять источник эффективного значения: `1c` или
   `map`; MAP не перезаписывает исходный факт 1C.
4. Разрешён будущий write-canary одной специально выбранной карточки в реальном
   tenant AlfaPro: stock `0`, один FBS warehouse, явный account allowlist и
   kill switch. Это не является разрешением создать ключ или карточку в M0–M2.
   Непосредственно перед внешним действием требуется отдельное подтверждение.
5. В коде нельзя hardcode-ить slug/ID tenant AlfaPro, Ozon Client-Id, warehouse
   ID или offer ID. Canary выбирается по точным DB identifiers в защищённой
   конфигурации rollout.

## Проверенный контракт Ozon Seller API

Основной источник: [официальная документация Ozon Seller API](https://docs.ozon.ru/api/seller/).
Документация и кабинет продавца проверены 2026-08-29. В кабинете AlfaPro есть
Client ID, но API key не создан и настройки кабинета не изменялись.

| Область | Метод Ozon | Контракт для MAP |
|---|---|---|
| Авторизация | headers `Client-Id`, `Api-Key` | Только server-to-server; секрет не попадает в browser, API response, log или telemetry |
| Права и срок ключа | `POST /v1/roles` | Сохранять expiry и доступные методы; до публикации требовать нужные роли |
| Данные продавца | `POST /v1/seller/info` | Проверить credentials и получить отображаемое имя/валюту аккаунта |
| Дерево типов | `POST /v1/description-category/tree` | Разрешать публикацию только в leaf type |
| Атрибуты | `POST /v1/description-category/attribute` | Versioned snapshot; `is_required` блокирует публикацию |
| Значения справочника | `POST /v1/description-category/attribute/values` | Хранить provider ID значения, а не только подпись |
| Создание/обновление | `POST /v3/product/import` | До 100 items; update отправляет полный payload; локально сохраняется `task_id` |
| Результат импорта | `POST /v1/product/import/info` | Poll task; принимать частичный результат по каждому item |
| Список товаров | `POST /v3/product/list` | Сверка по account и стабильному `offer_id` с пагинацией |
| Подробное состояние | `POST /v3/product/info/list` | Provider status, moderation errors, product ID, SKU, price и images |
| Цена | `POST /v1/product/import/prices` | Coalescing; не более 10 изменений цены товара в час; per-item result |
| Остаток FBS | `POST /v2/products/stocks` | До 100 product/warehouse pairs; один pair не чаще раза в 30 секунд; per-item result |
| Архив | `POST /v1/product/archive` | Сначала stock `0`, затем archive, затем обязательная read reconciliation |

Общий documented ceiling — 50 requests/s на Client ID, но для отдельных
методов действуют более строгие лимиты. MAP обязан учитывать response headers
rate limit/retry, вести budget отдельно по account и endpoint и не считать
общий ceiling разрешением отправлять burst.

Ozon работает в реальной среде без отдельного sandbox. Поэтому порядок rollout:
fake provider → deployed off → read-only real account → отдельное разрешение
write-canary.

## Инварианты мультитенантности и multi-account

Целевая иерархия:

```text
Tenant
├── Product
├── MarketplaceAccount (Avito #1..N)
│   └── Listing (product + exact account)
└── MarketplaceAccount (Ozon #1..N)
    ├── selected FBS Warehouse (MVP: ровно один)
    └── Listing (product + exact account)
        └── Ozon offer identity and provider operations
```

Обязательные fences для любого чтения и изменения:

- account выбирается одновременно по `pk`, `tenant` и ожидаемому marketplace;
- listing принадлежит тому же tenant, что product и account;
- provider operation содержит неизменяемые tenant/account/listing/provider;
- background task получает account ID и tenant ID и повторно проверяет их до
  любого provider I/O;
- provider ID никогда не используется как единственный tenant selector;
- account A не может читать или изменять provider state account B даже внутри
  одного tenant;
- один Ozon seller account не должен одновременно управляться из двух tenant
  без отдельного явно согласованного shared-ownership контракта;
- account rename не меняет `offer_id`, feed URL или другую внешнюю identity.

## Матрица общего и provider-specific контракта

| Область | Общее ядро MAP | Avito | Ozon |
|---|---|---|---|
| Account | tenant, provider, display name, external identity, active, encrypted credentials | OAuth client credentials, Autoload/profile/tariff, feed endpoint, адреса | Client-Id/API-Key, roles, key expiry, seller info, выбранный FBS warehouse |
| Product | article, name, brand, price, stock, images, neutral physical facts | OEM, Avito category/brand rules, address/contact defaults | barcode, dimensions, weight, VAT, description category/type, dictionary attributes |
| Listing | tenant + product + exact account, desired state, normalized remote observation | ad type, placement, XML/feed generation, Avito item ID/URL | stable offer ID, product ID, SKU, import task/result |
| Publish | explicit target account, preflight, durable operation, safe retry | durable feed intent and exact artifact already operational | `/v3/product/import` plus `/v1/product/import/info` reconciliation |
| Archive | desired archive plus verified provider outcome | remove from feed and poll Avito | stock 0 → archive → info reconciliation |
| Price | coalesced desired price, account/provider limits | current Avito update path | `/v1/product/import/prices`, product hourly limit |
| Stock | desired stock by account/warehouse | Avito feed semantics | `/v2/products/stocks`, product/warehouse pair throttling |
| Status | local desired state, operation state, provider observation are separate | feed delivery and Avito listing lifecycle | import task, moderation/product status and per-item errors |
| Statistics | marketplace/account/date dimensions | views, contacts, impressions | отдельная Ozon metric schema в O4; нельзя подставлять значения в Avito KPI |
| Logs | tenant, marketplace, account, operation, provider result, sanitized payload | feed run/profile/item events | roles/import/price/stock/archive/reconcile events |
| UI | provider and account context in URL | Autoload, feed, address and Avito field panels | roles/key expiry, warehouse, Ozon attributes and offer panels |

## Фактическое состояние backend

### Модели и identity

| Файл | Что уже подходит | Avito-specific предположение или риск |
|---|---|---|
| `apps/marketplaces/models.py` | `MarketplaceAccount` принадлежит tenant; unique `(tenant, marketplace, external_id)`; `Listing` unique `(tenant, product, account)` | choices содержит только Avito; account хранит Autoload/address/feed fields; `Listing.external_id` глобально unique и подписан как Avito ID; общие status labels содержат Avito; ad type/placement лежат в общей таблице |
| `apps/marketplaces/models.py` | `remote_status` и due/claim поля могут быть общими наблюдениями | `MarketplaceFeedRun`, endpoint/artifact/upload ledger являются контрактом Avito feed и не подходят для Ozon REST operations |
| `apps/marketplaces/models.py` | `ListingStats` tenant/listing/date scoped | поля и docstring описывают только Avito; provider/account dimension выводится косвенно через listing |
| `apps/sync/models.py` | `SyncLog` tenant-scoped и может ссылаться на product/listing | нет явных marketplace, account, operation и provider result; событие без listing нельзя надёжно отфильтровать по аккаунту |
| `apps/products/models.py` | article, OEM, brand, category, price, stock, warehouse и images уже есть | нет barcode, length/width/height, weight, VAT и field provenance `1c/map` |
| `apps/billing/models.py`, `apps/billing/services.py` | один tenant-wide `limit_listings`; usage считается по всем активным Listing | нет provider/account quota presentation; счётчик tenant cache может отставать и publish gate использует cache |

Решение для identity Ozon: не записывать `offer_id`, product ID или SKU в
глобально unique `Listing.external_id`. В O2/O3 вводится account-scoped Ozon
extension/identity с минимумом:

- immutable local `offer_id`;
- nullable provider `product_id` и `sku`;
- unique constraints с account, а не глобально;
- category/type и provider attribute payload;
- последняя подтверждённая provider revision/status.

`Listing.external_id` остаётся Avito compatibility field, пока отдельный пакет
не докажет безопасную миграцию к общей identity-модели.

### API, serializers и services

| Файл | Наблюдение M0 |
|---|---|
| `apps/marketplaces/base.py` | Абстракция упоминает feed и REST, но registry/capabilities отсутствуют; основной runtime её не использует как provider router |
| `apps/marketplaces/serializers.py` | Account serializer всегда строит `avito_status`/Autoload; write serializer всегда требует `client_id/client_secret`; Listing serializer отдаёт `can_check_avito_status` и запускает Avito preflight |
| `apps/marketplaces/views.py` | Account queryset всегда `select_related` Avito status/feed endpoint; PATCH содержит Avito placement/Autoload fields; listing list имеет account/status, но не marketplace; analytics не имеет account/marketplace filters |
| `apps/marketplaces/services.py` | Account create/update всегда проверяет Avito credentials; publication/archive/check-status импортируют Avito feed logic напрямую |
| `apps/marketplaces/services.py` | `publish_product` создаёт draft для **всех** активных marketplace accounts tenant — после добавления Ozon это станет неявным fan-out |
| `apps/marketplaces/tasks.py` | Provider operations и periodic jobs связаны с `avito_publish`/`avito_update`, `AvitoAdapter`, feed runs и Avito SyncLog messages |
| `apps/marketplaces/*urls.py` | Базовые account/listing/analytics URL нейтральны; `autoload-status` и brand refresh должны остаться Avito-only capability endpoints |
| `apps/products/views.py` | Product publish endpoint не принимает список target accounts и вызывает небезопасный для нескольких providers `publish_product` |
| `apps/sync/views.py` | Logs фильтруются только по event/status/date; marketplace/account/operation отсутствуют |
| `apps/analytics/services.py`, `apps/analytics/serializers.py` | Dashboard schema содержит `avito_total`, `avito` и Avito health warnings; provider-neutral summary отсутствует |

### Category и attribute mapping

Текущий `CategoryMapping` имеет marketplace discriminator, но `category_id` —
integer и один `attributes_map` на `tenant + marketplace + category_source`.
Для Ozon этого недостаточно: publication type и dictionary values versioned,
а один source category может требовать account-independent Ozon category/type
mapping и отдельные item-level values.

Avito tree/snapshots, brand catalog и JSON fixtures остаются внутри
`apps/marketplaces/adapters/avito/`. Ozon получает отдельные snapshots и sync
service; Avito migrations/data не переименовываются как подготовительный шаг.

### Notifications, webhooks и audit

- `NotificationService` нейтрален по каналу, но marketplace callers формируют
  Avito-specific text и не передают структурированный provider/account context.
- `SyncLog` сейчас является основным tenant-facing журналом, но не полноценным
  provider operation ledger.
- Общий webhook outbox в `apps/tenants/webhooks.py` существует, однако
  marketplace publication/status events в него сейчас не fan-out-ятся.
- Avito reconciliation audit и feed evidence нельзя переиспользовать для Ozon:
  Ozon mutations получают отдельный `MarketplaceOperation` с sanitized request
  digest, task ID, state, retries и reconciliation evidence.

## Фактическое состояние frontend

| Экран/файл | Avito-specific предположение |
|---|---|
| `frontend/src/app/dashboard/settings/page.tsx` | одна большая форма «Добавить аккаунт Avito»; любой account рендерится как Avito; Autoload, тариф, feed и addresses показаны без provider guard |
| `frontend/src/app/dashboard/listings/page.tsx` | account filter есть, marketplace filter/badge нет; статусы и действия содержат Avito wording; URL сохраняет listing/panel, но не provider context |
| `frontend/src/components/listings/ListingDrawer.tsx` | интерфейсы и панели содержат Avito preflight, OEM, brand catalog, ad type, placement, feed delivery и `can_check_avito_status` |
| `frontend/src/app/dashboard/logs/page.tsx` | фильтры только status/date; таблица не показывает marketplace, account и operation |
| `frontend/src/app/dashboard/analytics/page.tsx` | вся metric schema и empty state названы Avito; нет provider/account URL filters |
| `frontend/src/app/dashboard/page.tsx` | funnel и health cards используют «Отправка в Avito», `avito_total`, Autoload и Avito attention codes |
| `frontend/src/lib/api.ts` | account API exposes `checkAutoload`; listing API exposes generic path, но response contract Avito-specific |
| `frontend/src/lib/listing-publication.ts` | field order содержит Avito fields и alias `avito_oem`; общей provider schema нет |

Целевой layout:

- общий selector `marketplace` + `account` сохраняется в query string;
- Settings показывает provider cards `Avito` и `Ozon`, а внутри — список
  аккаунтов конкретного provider;
- Listings показывает marketplace badge, exact account и только доступные
  capabilities;
- Listing drawer имеет общий summary и отдельную provider section;
- Logs показывает provider/account/operation/result отдельными колонками;
- Analytics имеет общий счётчик листингов и provider-specific metric tabs;
- Avito Autoload/address/feed UI никогда не рендерится для Ozon account;
- Ozon roles/key expiry/warehouse/attributes никогда не рендерятся для Avito.

## Обязательная архитектурная граница

Нельзя реализовывать Ozon как ветвление внутри существующей Avito цепочки.

```text
Provider registry
├── avito capabilities
│   ├── account health / Autoload
│   ├── category and field preflight
│   └── existing durable feed delivery
└── ozon capabilities
    ├── account roles / seller info
    ├── description category / attributes
    ├── product import + task reconciliation
    └── price / stock / archive reconciliation
```

Общие capability contracts должны описывать результат, а не транспорт:

- `account_health`;
- `catalog_schema`;
- `publication_preflight`;
- `publish_or_update`;
- `price_update`;
- `stock_update`;
- `archive`;
- `status_reconcile`;
- `statistics`.

Avito facade делегирует существующему проверенному коду без переписывания feed
pipeline. Ozon adapter не создаёт `MarketplaceFeedRun` и не отправляется в
Avito queues.

## Состояния, которые нельзя смешивать

Для каждого Ozon listing отдельно хранятся:

1. **Desired state MAP** — что пользователь хочет опубликовать.
2. **Operation state** — queued/sending/outcome_unknown/reconciling/succeeded/
   partial/failed/manual_review.
3. **Provider observation** — фактический статус product/moderation в Ozon с
   timestamp и sanitized item errors.

Timeout после mutation не означает failure и не разрешает слепой повтор.
Сначала выполняется read reconciliation по account + stable offer ID/task ID.

## Первый implementation package: M1a

M1a — provider-neutral read/filter contract и UI context, без Ozon I/O,
migrations и смены Avito runtime. Это один пакет из двух подсистем
(marketplace API + dashboard UI). Фактически изменено 13 production-файлов,
что остаётся внутри repository gate `<= 20`.

Фактически изменённые production-файлы:

1. `apps/marketplaces/serializers.py` — добавить нейтральные marketplace,
   provider capabilities/details; сохранить Avito compatibility fields.
2. `apps/marketplaces/services.py` — усилить tenant fence read-query для
   listing/product/account даже при неконсистентных FK.
3. `apps/marketplaces/views.py` — marketplace/account filters и строгая
   проверка, что account принадлежит tenant/provider; без изменения mutations.
4. `apps/sync/serializers.py` — read-only marketplace/account presentation для
   логов, когда связь может быть доказана через listing.
5. `apps/sync/views.py` — marketplace/account filters с tenant fence.
6. `frontend/src/components/marketplaces/MarketplaceAccountFilter.tsx` — общий
   selector, сериализуемый в URL.
7. `frontend/src/lib/dashboard-query.ts` — нормализация marketplace/account URL
   params без принятия неизвестных или неположительных значений.
8. `frontend/src/app/dashboard/listings/page.tsx` — marketplace badge и filters.
9. `frontend/src/app/dashboard/logs/page.tsx` — provider/account columns/filters.
10. `frontend/src/app/dashboard/analytics/page.tsx` — provider/account context и
    provider-specific empty state; backend analytics filter входит в пункт 3.
11. `frontend/src/app/dashboard/settings/page.tsx` — отдельные Avito/Ozon cards;
    Ozon card в M1a только `not connected / coming next`, без credential form.
12. `frontend/src/components/listings/ListingDrawer.tsx` — provider guard вокруг
    существующих Avito panels; нового Ozon editor ещё нет.
13. `frontend/src/app/dashboard/page.tsx` — neutral headings и Avito health как
    отдельная provider card.

Планируемые test-файлы:

- `apps/marketplaces/tests/test_provider_neutral_contract.py`;
- `apps/sync/tests/test_views.py`;
- существующие account/listing API regressions;
- frontend unit tests для URL filters, conditional panels и multi-account.

M1a обязательно исправляет presentation/read paths, но **не** добавляет `ozon`
в model choices. Нельзя создавать Ozon account, пока O1 не добавит provider
credentials, health и fake-provider acceptance. Product publish fan-out в M1a
не расширяется; его mutation contract меняется отдельным bounded M1b до O1.

## Пакеты расширения

### M1b — безопасный target selection для mutations (`ENABLED`)

- product publish принимает явный список account IDs;
- archive/bulk actions подтверждают точный marketplace/account scope;
- отсутствует implicit «во все активные аккаунты»;
- provider registry/capabilities вводится без переписывания Avito feed;
- Avito regression и multi-account/cross-tenant acceptance обязательны.

### O1 — account connection, deployed off

- добавить Ozon provider choice и provider-specific encrypted credential schema;
- validate через `/v1/roles` и `/v1/seller/info`;
- Ozon account health/key expiry/role diagnostics;
- fake API, 401/403/429/5xx/timeout tests;
- реальный AlfaPro только read-only после отдельного разрешения.

Минимальные роли read-only canary: Company, Description Category,
Product read-only и Warehouse. Роль Product добавляется только перед отдельно
подтверждённой публикацией.

### O2 — product facts, catalog и preflight

- neutral physical profile с barcode/dimensions/weight/VAT и provenance 1C/MAP;
- versioned Ozon category/type/attribute/value snapshots;
- Ozon offer extension с stable offer ID;
- field-level UI и fail-closed required field preflight.

### O3 — durable publication и reconciliation

- `MarketplaceOperation`, per-item outcome, task polling и safe retry;
- no blind retry после timeout;
- один write-canary AlfaPro, stock 0, один FBS warehouse;
- account allowlist и kill switch.

### O4 — price, stock, archive, analytics и rollout

- coalescing и endpoint/account/product-warehouse rate limits;
- price/stock/archive reconciliation;
- provider metrics, alerts и runbook;
- один account → второй Ozon account того же tenant → новый tenant → fleet.

## M0 gate

Фактически выполнено 2026-08-29:

```text
git diff --check -- docs/MARKETPLACE_EXPANSION_ROADMAP.md
результат: exit 0

проверка trailing whitespace обоих документов
результат: exit 0

проверка относительных Markdown-ссылок двух документов
результат: exit 0, отсутствующих целей нет

git status --short -- docs/MARKETPLACE_INTEGRATION_INVENTORY.md \
  docs/MARKETPLACE_EXPANSION_ROADMAP.md
результат: один новый и один изменённый документ; runtime, migrations и tests
не изменены
```

Backend/frontend tests для M0 не запускались: runtime-код не изменён. Следующий
пакет нельзя начинать без отдельной активации M1a.

## M1a gate

Фактически выполнено 2026-08-29:

```text
python3 -m compileall -q apps/marketplaces/serializers.py \
  apps/marketplaces/services.py apps/marketplaces/views.py \
  apps/marketplaces/tests/test_provider_neutral_contract.py \
  apps/sync/serializers.py apps/sync/views.py apps/sync/tests/test_views.py
результат: exit 0

python3 -m flake8 apps/marketplaces/serializers.py \
  apps/marketplaces/services.py apps/marketplaces/views.py \
  apps/marketplaces/tests/test_provider_neutral_contract.py \
  apps/sync/serializers.py apps/sync/views.py apps/sync/tests/test_views.py
результат: exit 0

cd frontend && npm run typecheck
результат: exit 0

cd frontend && npm run lint
результат: exit 0

cd frontend && npm run test:unit
результат: exit 0, 39 passed

cd frontend && npm run build -- --webpack
результат: exit 0, production build и 21 page generated

pytest -q apps/marketplaces/tests/test_provider_neutral_contract.py \
  apps/marketplaces/tests/test_account_api.py \
  apps/marketplaces/tests/test_listing_patch_api.py \
  apps/sync/tests/test_views.py \
  apps/marketplaces/tests/test_services.py
результат: exit 0, 46 passed in 21.70s
контур: временные local PostgreSQL/Redis и settings module без Admin UI и Beat;
версии Django 5.2.17, pytest 9.1.1, pytest-django 4.13.0

python manage.py makemigrations --check --dry-run
результат: exit 0, No changes detected

python manage.py check
результат: exit 0, System check identified no issues

git diff --check
результат: exit 0
```

До PR штатная локальная команда
`docker compose exec django pytest -q
apps/marketplaces/tests/test_provider_neutral_contract.py
apps/marketplaces/tests/test_account_api.py
apps/marketplaces/tests/test_listing_patch_api.py apps/sync/tests/test_views.py
apps/marketplaces/tests/test_services.py` до сбора тестов вернула Docker Desktop
`unable to start`. Полный CI PR `#274` затем закрыл этот gate на GitHub:
backend shards, coverage, schema/supply-chain, frontend и production
image/security завершились успешно.

Production evidence:

```text
PR: #274
release SHA: 0b84c9a1d209e82fb730fc0abd85c31ff9cd6178
PR full CI run: 33269221495 — success
main exact-tree CI run: 33269683305 — success
Deploy run: 33269694167 — success, 3m4s
encrypted pre-migration backup: uploaded
migrations: No migrations to apply
services: db, Redis, broker, proxy, Django, workers, Beat, frontend, Nginx healthy
topology: production topology is healthy and exact
public readiness: четыре последовательных HTTP 200 за минуту
PROD_DEPLOY_ENABLED: восстановлен в false после запуска exact-SHA release
```

M1a включён без feature flag и имеет статус `ENABLED`. Подключение Ozon,
credentials и внешний API I/O не входят в M1a/M1b и остаются выключены до O1.

## M1b gate

Локально выполнено 2026-08-29:

```text
python3 -m compileall -q <изменённые backend production-файлы>
результат: exit 0

docker compose exec django flake8 <изменённые backend-файлы>
результат: exit 0

docker compose exec django mypy
результат: exit 0, no issues found in 699 source files

docker compose exec django mypy --check-untyped-defs \
  --exclude '(^|/)(tests?|migrations)/' apps config backup
результат: exit 0, no issues found in 351 source files

docker compose exec django pytest -q \
  apps/marketplaces/tests/test_provider_neutral_contract.py \
  apps/marketplaces/tests/test_listing_review.py \
  apps/marketplaces/tests/test_avito.py \
  apps/marketplaces/tests/test_status_fencing.py \
  apps/marketplaces/tests/test_feed_intent_local_writers.py \
  apps/marketplaces/tests/test_listing_patch_api.py \
  apps/tenants/tests/test_api_key_authorization.py
результат: exit 0, 276 passed in 68.28s; одно teardown warning тестовой БД

docker compose exec django python manage.py spectacular \
  --file /tmp/openapi-schema.yml --validate --fail-on-warn
результат: exit 0

docker compose exec django python manage.py makemigrations --check --dry-run
результат: exit 0, No changes detected

cd frontend && npm run typecheck
результат: exit 0

cd frontend && npm run lint
результат: exit 0

cd frontend && npm run test:unit
результат: exit 0, 39 passed

docker compose run --rm frontend npm run build
результат: exit 0, production build и 21 page generated

git diff --check -- . ':!.claude/settings.local.json'
результат: exit 0
```

M1b не добавляет model choices, credentials или внешний Ozon I/O. Avito feed
runtime, P7 и production flags не меняются.

Production evidence:

```text
PR: #276
code commit: 6f0e344d2a38b7588937f300c515949c74d7e780
release SHA: 58d152ccc6975bb97615f5b6f14d5a2a6b6e046b
PR full CI run: 33271688562 — success
backend shards: 8m43s, 6m41s, 8m23s — success
contracts/schema/supply-chain: 2m32s — success
production images/runtime security: 2m36s — success
frontend checks/build: 1m9s — success
combined backend coverage: 50s — success
main exact-tree CI run: 33272146435 — success
Deploy run: 33272159684 — success, 3m2s
encrypted pre-migration backup: uploaded
migrations: No migrations to apply
services: db, Redis, broker, proxy, Django, workers, Beat, frontend, Nginx healthy
topology: production topology is healthy and exact
public readiness: четыре последовательных HTTP 200 за 45 секунд
frontend /dashboard/settings: HTTP 200
PROD_DEPLOY_ENABLED: восстановлен в false после старта exact-SHA release
```

M1b включён без feature flag и имеет статус `ENABLED`. Ozon model choice,
credentials, API I/O и кабинет AlfaPro не изменялись; они остаются выключены
до отдельного пакета O1.
