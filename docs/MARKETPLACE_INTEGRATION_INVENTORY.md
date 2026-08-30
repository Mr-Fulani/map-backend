# Инвентаризация контрактов маркетплейсов: Avito и Ozon

Обновлено: 2026-08-30.

Статус: M0 `VERIFIED`, M1a `ENABLED`, M1b `ENABLED`,
O1a/O1b `DEPLOYED_OFF`.
Документ описывает развитие репозитория от commit
`fde9564f7ed183c30d852a4024a7957a667fee42` до production release O1b
`3ad929ee7b5a6acabc06b8c70070c66cadf20786` 2026-08-30.

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
| Права и срок ключа | `POST /v1/roles` | Сохранять возвращённый `expires_at`, роли и разрешённые API methods; до публикации требовать нужные роли |
| Данные продавца | `POST /v1/seller/info` | Проверить credentials и получить отображаемое имя/валюту аккаунта |
| Склады | `POST /v2/warehouse/list` | Получить FBS warehouses с bounded cursor pagination; один склад выбрать автоматически, 0 или >1 вернуть как явное состояние onboarding |
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

Ozon сообщил, что для ключей, создаваемых начиная с 2026-09-03, срок действия
изменится с шести до трёх месяцев. Поэтому MAP не вычисляет срок по дате
создания, а сохраняет точный `expires_at` из `/v1/roles`. Источники:
[уведомление о поле `expires_at`](https://t.me/s/OzonSellerAPI?before=637) и
[уведомление об изменении срока ключей](https://t.me/s/ozonsellerapi).

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

### O1 — account connection

#### O1a — backend account foundation (`DEPLOYED_OFF`)

- Ozon provider choice и provider-specific encrypted credential schema;
- validate через `/v1/roles`, `/v1/seller/info` и `/v2/warehouse/list`;
- профиль health/key expiry/roles/sanitized API methods/warehouse;
- глобальная защита от подключения одного Client-Id в разные tenant при
  поддержке нескольких разных Ozon accounts одного tenant;
- fixed-origin adapter без redirects, с deadlines, bounded responses/cursors и
  безопасными ошибками для 401/403/429/5xx/timeout;
- fake-provider acceptance; production feature flag по умолчанию `false`.

Фактический scope O1a — backend и конфигурация, без Ozon UI, periodic health
polling и реального API I/O. Ozon-specific логика находится в отдельных
`adapters/ozon/client.py` и `ozon_account_connection.py`; Avito feed runtime,
P7 и production feed flags не менялись.

#### O1b — безопасный UI/onboarding (`DEPLOYED_OFF`)

- отдельная Ozon account card/list, не смешанная с Autoload/Avito panels;
- Client ID/API key принимаются write-only, не сохраняются в persistent browser
  storage и не возвращаются из read API;
- tenant видит connection state, роли, expiry и выбранный warehouse;
- отдельный read-only rollout endpoint не вызывает provider и fail-closed
  управляет доступностью форм;
- provider-specific error allowlist не отражает неизвестный response text;
- Ozon появляется в общем marketplace filter только при наличии Ozon account;
- production release выключен через
  `OZON_ACCOUNT_CONNECTION_ENABLED=false`.

#### O1c — read-only AlfaPro canary (`REQUIRES APPROVAL`)

- непосредственно перед созданием/использованием ключа требуется отдельное
  подтверждение пользователя;
- account allowlist и read-only вызовы roles/seller/warehouses;
- ни одного product/price/stock/archive mutation;
- health polling и дальнейший rollout только после результатов canary.

Минимальные роли read-only canary: Company, Description Category,
Product read-only и Warehouse. Роль Product добавляется только перед отдельно
подтверждённой публикацией.

### O2 — product facts, catalog и preflight

- neutral physical profile с barcode/dimensions/weight/VAT и provenance 1C/MAP;
- versioned Ozon category/type/attribute/value snapshots;
- Ozon offer extension с stable offer ID;
- field-level UI и fail-closed required field preflight.

Первый bounded пакет O2a ограничен versioned tree/attribute snapshots и
ручным tenant/account-scoped read-only API. Dictionary values, физические поля,
offer extension, UI и preflight остаются в O2b/O2c; Avito runtime не меняется.

O2a-UI закрывает только видимость foundation: показывает локальные метаданные
снимка конкретного Ozon-аккаунта, даёт вручную подтвердить read-only refresh
дерева и сохраняет Ozon/account в provider-neutral URL-фильтрах. Выбор типов,
dictionary values, физические поля, offer extension и preflight остаются в
O2b/O2c; фоновых чтений Ozon и изменений Avito runtime нет.

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

M1a включён без feature flag и имеет статус `ENABLED`. На момент этого release
подключение Ozon, credentials и внешний API I/O ещё не входили в runtime.

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

M1b включён без feature flag и имеет статус `ENABLED`. В самом M1b Ozon model
choice, credentials, API I/O и кабинет AlfaPro не изменялись; backend foundation
добавлен последующим отдельным пакетом O1a.

## O1a gate

Локально выполнено 2026-08-29:

```text
docker compose exec django pytest -q \
  apps/marketplaces/tests/test_ozon_account_api.py \
  apps/marketplaces/tests/test_ozon_client.py \
  apps/marketplaces/tests/test_provider_neutral_contract.py \
  tests/test_settings_resource_caps.py
результат: exit 0, 44 passed in 27.72s

docker compose exec django pytest -q \
  apps/marketplaces/tests/test_ozon_account_api.py \
  apps/marketplaces/tests/test_ozon_client.py \
  apps/marketplaces/tests/test_provider_neutral_contract.py \
  apps/marketplaces/tests/test_listing_review.py \
  apps/marketplaces/tests/test_avito.py \
  apps/marketplaces/tests/test_status_fencing.py \
  apps/marketplaces/tests/test_feed_intent_local_writers.py \
  apps/marketplaces/tests/test_listing_patch_api.py \
  apps/tenants/tests/test_api_key_authorization.py \
  tests/test_settings_resource_caps.py
результат: exit 0, 313 passed in 69.61s

docker compose exec django flake8 .
результат: exit 0

docker compose exec django mypy
результат: exit 0, no issues found in 706 source files

docker compose exec django mypy --check-untyped-defs \
  --exclude '(^|/)(tests?|migrations)/' apps config backup
результат: exit 0, no issues found in 355 source files

docker compose exec django python manage.py spectacular \
  --file /tmp/openapi-schema.yml --validate --fail-on-warn
результат: exit 0

docker compose exec django python manage.py makemigrations --check --dry-run
результат: exit 0, No changes detected

git diff --check -- . ':!.claude/settings.local.json'
результат: exit 0
```

Scope evidence:

```text
code commit: e1dda3a35b40a55fdfebb8bd4631671d57942da3
files: 16
diff: 1 443 additions, 25 deletions
migrations: 1 (marketplaces.0032_ozon_account_profile)
repository limits: соблюдены
```

Production evidence:

```text
PR: #278
release SHA: e5ccef76e8e93677e196f9d10c1b63f763f239bc
PR full CI run: 33273736364 — success
backend shards: 8m16s, 7m23s, 8m7s — success
contracts/schema/supply-chain: 2m56s — success
production images/runtime security: 2m33s — success
frontend checks/build: 1m20s — success
combined backend coverage: 57s — success
main exact-tree CI run: 33274177354 — success
Deploy run: 33274191198 — success, 2m33s
encrypted pre-migration backup: uploaded
migrations: Applying marketplaces.0032_ozon_account_profile... OK
services: db, Redis, broker, proxy, Django, workers, Beat, frontend, Nginx healthy
topology: production topology is healthy and exact
public readiness: HTTP 200 x4 (0.776s, 0.414s, 0.381s, 0.503s)
frontend /dashboard/settings: HTTP 200 (0.519s)
PROD_DEPLOY_ENABLED: восстановлен в false после старта exact-SHA release
```

O1a имеет статус `DEPLOYED_OFF`: migration и backend-контракт находятся на
production, но `OZON_ACCOUNT_CONNECTION_ENABLED=false` по умолчанию блокирует
Ozon connection/API I/O. Реальный API key не создавался, credentials AlfaPro не
сохранялись, запросов к Ozon от MAP не было, кабинет продавца не изменялся.
Следующий отдельный пакет O1b добавил UI/onboarding, сохранив этот dark launch.

## O1b gate

Локально выполнено 2026-08-30:

```text
docker compose exec django pytest -q \
  apps/marketplaces/tests/test_ozon_account_api.py
результат: exit 0, 10 passed in 18.35s

docker compose exec django pytest -q \
  apps/marketplaces/tests/test_ozon_account_api.py \
  apps/marketplaces/tests/test_ozon_client.py \
  apps/marketplaces/tests/test_provider_neutral_contract.py \
  apps/marketplaces/tests/test_listing_review.py \
  apps/marketplaces/tests/test_avito.py \
  apps/marketplaces/tests/test_status_fencing.py \
  apps/marketplaces/tests/test_feed_intent_local_writers.py \
  apps/marketplaces/tests/test_listing_patch_api.py \
  apps/tenants/tests/test_api_key_authorization.py \
  tests/test_settings_resource_caps.py
результат: exit 0, 301 passed in 71.92s

docker compose exec django pytest -q \
  apps/marketplaces/tests/test_account_api.py
результат: exit 0, 14 passed in 19.54s

docker compose exec django flake8 \
  apps/marketplaces/views.py apps/marketplaces/account_urls.py \
  apps/marketplaces/tests/test_ozon_account_api.py
результат: exit 0

docker compose exec django mypy
результат: exit 0, no issues found in 706 source files

docker compose exec django mypy --check-untyped-defs \
  --exclude '(^|/)(tests?|migrations)/' apps config backup
результат: exit 0, no issues found in 355 source files

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
результат: exit 0, 43 passed

git diff --cached --check
результат: exit 0
```

Локальный `docker compose run --rm frontend npm run build` успешно прошёл
compile, TypeScript и static generation `21/21`, но после завершения Next.js
Docker Desktop вернул infrastructure exit `125`: metadata DB не смогла
записаться при заполненном диске. Это не засчитано как локальный clean build;
GitHub CI повторил production build в чистой среде и завершил его успешно.

Scope evidence:

```text
code commit: 197d81de170526af13d67d82f9c166c927887803
files: 11 (8 production, 2 tests, 1 test config)
diff: 796 additions, 80 deletions
migrations: 0
repository limits: соблюдены
```

Production evidence:

```text
PR: #280
release SHA: 3ad929ee7b5a6acabc06b8c70070c66cadf20786
PR full CI run: 33275309364 — success
backend shards: 8m7s, 8m39s, 8m15s — success
contracts/schema/supply-chain: 3m2s — success
production images/runtime security: 2m43s — success
frontend checks/build: 56s — success
combined backend coverage: 53s — success
main exact-tree CI run: 33275765742 — success
Deploy run: 33275780372 — success, 3m7s
encrypted pre-migration backup: uploaded
migrations: No migrations to apply
services: db, Redis, broker, proxy, Django, workers, Beat, frontend, Nginx healthy
topology: production topology is healthy and exact
public readiness: HTTP 200 x4 (0.493s, 0.440s, 0.241s, 0.337s)
frontend /dashboard/settings: HTTP 200 (0.418s)
PROD_DEPLOY_ENABLED: восстановлен в false после старта exact-SHA release
```

O1b имеет статус `DEPLOYED_OFF`: отдельная Ozon-карточка и безопасный UI уже на
production, но rollout endpoint возвращает disabled при production default
`OZON_ACCOUNT_CONNECTION_ENABLED=false`; форма не принимает API key и MAP не
вызывает Ozon. API key не создавался, credentials AlfaPro не сохранялись,
кабинет продавца не изменялся. Следующий пакет — O1c read-only AlfaPro canary;
перед созданием/использованием ключа требуется отдельное подтверждение.

## O1c safety gate

Локально выполнено 2026-08-30:

```text
docker compose exec django pytest -q \
  apps/marketplaces/tests/test_ozon_account_api.py \
  tests/test_settings_resource_caps.py
результат: exit 0, 24 passed in 34.73s

docker compose exec django pytest -q \
  apps/marketplaces/tests/test_ozon_account_api.py \
  apps/marketplaces/tests/test_ozon_client.py \
  apps/marketplaces/tests/test_provider_neutral_contract.py \
  apps/marketplaces/tests/test_listing_review.py \
  apps/marketplaces/tests/test_avito.py \
  apps/marketplaces/tests/test_status_fencing.py \
  apps/marketplaces/tests/test_feed_intent_local_writers.py \
  apps/marketplaces/tests/test_listing_patch_api.py \
  apps/tenants/tests/test_api_key_authorization.py \
  tests/test_settings_resource_caps.py
результат: exit 0, 310 passed in 120.71s; один teardown warning локальной
test DB из-за оставшихся параллельных sessions, полный GitHub gate чистый

docker compose exec django pytest -q \
  apps/marketplaces/tests/test_account_api.py
результат: exit 0, 14 passed in 42.53s

docker compose exec django flake8 \
  config/settings/base.py \
  apps/marketplaces/ozon_rollout.py \
  apps/marketplaces/ozon_account_connection.py \
  apps/marketplaces/serializers.py \
  apps/marketplaces/views.py \
  apps/marketplaces/tests/test_ozon_account_api.py \
  tests/test_settings_resource_caps.py
результат: exit 0

docker compose exec django mypy
результат: exit 0, no issues found in 707 source files

docker compose exec django mypy --check-untyped-defs \
  --exclude '(^|/)(tests?|migrations)/' apps config backup
результат: exit 0, no issues found in 356 source files

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
результат: exit 0, 43 passed

docker compose run --rm frontend npm run build
результат: exit 0, compile/TypeScript/static generation 21/21 successful

git diff --cached --check
результат: exit 0
```

Scope evidence:

```text
code commit: eb30b0353e09693de62db87191740ccf7c02dbd3
files: 9 (7 production/config, 2 tests)
diff: 332 additions, 8 deletions
migrations: 0
repository limits: соблюдены
```

Production evidence:

```text
PR: #282
release SHA: 90324efe4ac79c65d3cff79d584cfd8583ba1e1d
PR full CI run: 33277471000 — success
backend shards: 10m32s, 7m21s, 8m19s — success
contracts/schema/supply-chain: 3m12s — success
production images/runtime security: 2m30s — success
frontend checks/build: 1m26s — success
combined backend coverage: 48s — success
main exact-tree CI run: 33278008805 — success
Deploy run: 33278019318 — success, 3m6s
encrypted pre-migration backup: uploaded
migrations: No migrations to apply
services: db, Redis, broker, proxy, Django, workers, Beat, frontend, Nginx healthy
topology: production topology is healthy and exact
public readiness: HTTP 200 x4 (0.310s, 0.370s, 0.219s, 0.249s)
frontend /dashboard/settings: HTTP 200 (0.354s)
PROD_DEPLOY_ENABLED: восстановлен в false после старта exact-SHA release
```

O1c safety gate имеет статус `DEPLOYED_OFF`: exact tenant slug и Ozon Client ID
требуются одновременно с глобальным флагом, а каждый запрос с ключом требует
явного read-only подтверждения. Production rollout остаётся выключенным;
реальный API key не создавался и не использовался, credentials AlfaPro не
сохранялись, MAP не обращался к Ozon и кабинет продавца не изменялся. Live
AlfaPro canary требует отдельного подтверждения непосредственно перед внешним
действием и отдельной production-конфигурации точных allowlists.
