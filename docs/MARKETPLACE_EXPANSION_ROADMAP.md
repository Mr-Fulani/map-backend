# Roadmap разделения маркетплейсов и подключения Ozon

Обновлено: 2026-08-30.

## Решение

P7 Avito не блокирует развитие продукта: он относится к retention и удалению
старых служебных данных. Следующая продуктовая цель — подготовить MAP к двум
маркетплейсам и только затем подключать Ozon к уже разделённому интерфейсу.

Ozon нельзя добавлять набором условных `if marketplace == ...` поверх экранов,
названий и статусов Avito. Сначала выделяется provider-neutral слой, при этом
текущий Avito runtime и его feed-флаги не меняются.

## M0 — инвентаризация контрактов — `VERIFIED` 2026-08-29

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

Gate закрыт в
[MARKETPLACE_INTEGRATION_INVENTORY.md](MARKETPLACE_INTEGRATION_INVENTORY.md):
зафиксированы решения по multi-account, одному FBS warehouse, fallback 1C/MAP,
реальному AlfaPro canary, проверенные методы Ozon API, текущие backend/frontend
риски и точный состав M1a. Изменена только документация; migrations и runtime
не затронуты.

## M1 — provider-neutral интерфейс — M1a/M1b `ENABLED` 2026-08-29

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

Чтобы пакет оставался проверяемым, M1 выполняется двумя последовательными
частями:

- **M1a** — provider/account presentation, URL filters и conditional panels;
  без migrations, mutations и значения `ozon` в model choices;
- **M1b** — явный выбор target accounts для product/bulk mutations и provider
  capability registry; implicit публикация во все активные аккаунты удаляется
  до подключения Ozon.

Точный файловый состав и non-goals M1a/M1b находятся в
[MARKETPLACE_INTEGRATION_INVENTORY.md](MARKETPLACE_INTEGRATION_INVENTORY.md).

M1a реализован в границах read/presentation слоя: API и Dashboard получили
marketplace/account context, tenant-fenced filters и fail-closed presentation
для ещё не подключённого provider. Avito mutations, feed runtime, model choices
и production flags не менялись; migrations и Ozon API I/O отсутствуют.

Целевой backend gate прошёл: 46 тестов marketplace/sync, включая новые
provider-neutral, cross-tenant и Avito regression contracts. Frontend прошёл
unit, typecheck, ESLint и production webpack build. `makemigrations --check
--dry-run` не обнаружил изменений схемы.

Полный CI PR `#274` закрыл backend shards, coverage, schema/supply-chain,
frontend и production image/security gates. M1a выложен и включён на production
SHA `0b84c9a1d209e82fb730fc0abd85c31ff9cd6178`: encrypted backup создан,
миграций к применению не было, все production services и topology healthy,
public readiness стабильно отвечает HTTP 200. На момент релиза M1a Ozon
account connection, API I/O и credentials оставались выключены до M1b/O1.

## O1 — безопасное подключение Ozon Seller API — O1a/O1b/O1c gate `DEPLOYED_OFF`

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

O1 выполняется тремя последовательными частями:

- **O1a — backend account foundation (`DEPLOYED_OFF`)**: provider choice,
  encrypted credentials, глобальная защита повторного Client-Id,
  `OzonAccountProfile`, безопасный read-only adapter для `/v1/roles`,
  `/v1/seller/info` и `/v2/warehouse/list`, fake-provider acceptance и
  feature flag `OZON_ACCOUNT_CONNECTION_ENABLED=false`;
- **O1b — безопасный UI/onboarding (`DEPLOYED_OFF`)**: отдельная Ozon-карточка
  и список аккаунтов, write-only ввод Client ID/API key без persistent browser
  storage, роли/expiry/warehouse и понятные provider-specific ошибки;
- **O1c — read-only AlfaPro canary (`GATE DEPLOYED_OFF`, live canary pending)**:
  отдельное подтверждение перед созданием или использованием ключа, точные
  tenant/Client-ID allowlists, проверка ролей/срока/продавца и одного FBS-склада
  без product mutations. Health polling активируется только после успешного
  canary и отдельного review.

O1a реализован отдельным Ozon adapter/service и не встроен в Avito feed path.
Один tenant может иметь несколько разных Ozon accounts, но одинаковый Ozon
Client-Id нельзя подключить к двум tenant. Если API возвращает ровно один
склад, он выбирается автоматически; при нуле или нескольких складах account
переходит в явное диагностическое состояние. Срок ключа хранится только из
ответа Ozon, без локального расчёта.

Локальный gate O1a: 44 focused теста и 313 Ozon/Avito/provider-neutral
regression тестов прошли; `flake8`, baseline и strict `mypy`, OpenAPI
validation, `makemigrations --check --dry-run` и `git diff --check` завершились
успешно. Изменено 16 файлов, 1 443 добавления, одна migration — внутри
repository limits.

Полный CI PR `#278` и exact-tree CI на `main` прошли все backend, contracts,
schema/supply-chain, frontend и production image/security gates. O1a выложен
на production SHA `e5ccef76e8e93677e196f9d10c1b63f763f239bc`: перед migration создан
encrypted backup, `marketplaces.0032_ozon_account_profile` применена, все
production services и topology healthy, четыре внешних readiness-запроса и
страница `/dashboard/settings` ответили HTTP 200. Deploy gate возвращён в
`false`.

O1b добавил отдельный, видимый в Dashboard Ozon account UI: список нескольких
кабинетов, безопасный профиль seller/company, роли, точный expiry, состояние и
единственный FBS-склад, а также формы подключения и ротации ключа. UI получает
rollout-state из отдельного tenant-authenticated GET, который никогда не
обращается к provider. Неизвестный или некорректный rollout-ответ трактуется
как `disabled`; неизвестный текст provider error не отражается пользователю.
Секрет не хранится в React/local/session storage: после submit форма сразу
очищается и unmount-ится, а read API credentials не возвращает.

Локальный gate O1b: Ozon account/rollout — 10 тестов, широкий
Ozon/Avito/provider-neutral regression — 301 тест, общий account API — 14
тестов; frontend typecheck, ESLint и 43 unit-теста прошли. Baseline/strict
`mypy`, OpenAPI validation, `makemigrations --check --dry-run` и
`git diff --check` также успешны. Изменено 11 файлов, 796 добавлений, migrations
нет — внутри repository limits. Локальный container build скомпилировал и
сгенерировал 21/21 страниц, после чего переполненный Docker Desktop завершил
ожидание контейнера infrastructure exit 125; чистый GitHub frontend build
полностью прошёл за 56 секунд.

Полный CI PR `#280` прошёл все backend shards, coverage, contracts,
schema/supply-chain, frontend и production image/security gates. O1b выложен на
production SHA `3ad929ee7b5a6acabc06b8c70070c66cadf20786`: encrypted backup создан,
новых migrations нет, все services и topology healthy, четыре внешних
readiness-запроса и `/dashboard/settings` ответили HTTP 200. Deploy gate
возвращён в `false`.

O1a/O1b не создавали API key, не сохраняли credentials AlfaPro, не обращались
к реальному Ozon API и не изменяли кабинет продавца. На production
`OZON_ACCOUNT_CONNECTION_ENABLED=false`, поэтому кнопка подключения и формы
credentials fail-closed. Следующий пакет — O1c read-only AlfaPro canary с
tenant/account allowlist и отдельным подтверждением перед внешним действием.

O1c safety gate добавляет два независимых точных allowlist — tenant slug и
Ozon Client ID — поверх глобального флага, а backend и UI требуют явное
подтверждение read-only проверки в каждом запросе с ключом. Пустой, повреждённый
или несортированный allowlist закрывает доступ; rollout endpoint возвращает
`false` всем неразрешённым tenant. Проверка остаётся ограниченной `/v1/roles`,
`/v1/seller/info` и `/v2/warehouse/list`; endpoints товаров, цен, остатков и
архивации в пакет не добавлялись.

Локальный gate O1c: 24 focused теста, 310 Ozon/Avito/provider-neutral regression
тестов и 14 тестов общего account API прошли; frontend typecheck, ESLint, 43
unit-теста и чистый Docker build с генерацией 21/21 страниц успешны. Flake8,
baseline mypy для 707 файлов, strict mypy для 356 файлов, OpenAPI validation,
`makemigrations --check --dry-run` и `git diff --cached --check` также успешны.
Изменено 9 файлов, 332 добавления, migrations нет — внутри repository limits.

Полный CI PR `#282` прошёл все backend shards, coverage, contracts,
schema/supply-chain, frontend и production image/security gates. O1c safety gate
выложен на production SHA `90324efe4ac79c65d3cff79d584cfd8583ba1e1d`:
encrypted backup создан, новых migrations нет, все services и topology healthy,
четыре внешних readiness-запроса и `/dashboard/settings` ответили HTTP 200.
Deploy gate возвращён в `false`.

Live canary ещё не выполнялся: API key не создавался и не использовался,
credentials AlfaPro не сохранялись, запросов от MAP к Ozon не было и кабинет
продавца не изменялся. Следующая контрольная точка — отдельное подтверждение
непосредственно перед настройкой точных production allowlists и read-only
проверкой одного кабинета AlfaPro.

## O2 — каталог, категории и обязательные атрибуты

Результат: товар можно подготовить к Ozon без влияния на Avito-карточку.

- Ozon category/attribute dictionaries и их versioned sync;
- отдельный mapping товара в Ozon offer;
- field-level preflight с разделением ошибок и рекомендаций;
- изображения, barcode, dimensions, VAT и warehouse requirements;
- provider-specific drawer sections без смешения полей Avito/Ozon.

Gate: schema drift tests, representative category fixtures, fail-open только
для необязательных рекомендаций и fail-closed для обязательных provider fields.

O2 выполняется отдельными последовательными пакетами:

- **O2a — read-only catalog schema foundation**: ручное account-scoped чтение
  дерева категорий и характеристик, нормализация с жёсткими лимитами,
  versioned snapshots и локальный status API; без UI, background tasks и
  product mutations;
- **O2a-UI — безопасное закрытие foundation**: account-scoped локальный статус
  снимка и ручное подтверждённое обновление дерева в карточке Ozon, плюс
  сохранение Ozon/account в общих provider-neutral фильтрах; без автоматических
  provider reads и без product mutations;
- **O2b — physical facts и provenance**: barcode, dimensions, weight и VAT с
  приоритетом валидного значения 1C и fallback в MAP;
- **O2c1 — identity, категория и базовый preflight**: stable offer identity на
  точный Ozon-аккаунт, выбор leaf type и проверка общих данных товара;
- **O2c2 — характеристики Ozon**: account/category-scoped схема атрибутов,
  dictionary values и field-level ошибки обязательных provider-полей.

O2a и O2a-UI не используют Avito feed/services/tasks, не меняют Listing/Product
и не вызывают product/price/stock/archive методы Ozon. Реальный catalog refresh
остаётся отдельным ручным read-only действием точного tenant/account.

O2a foundation и O2a-UI выложены на production 2026-08-30. Read-only canary
AlfaPro сохранил отдельный account-scoped снимок: 9 796 узлов, 8 876 активных
типов, revision `0985c3c51042`; после полной перезагрузки страницы выбранные
Ozon и аккаунт сохранились. Product/price/stock/archive методы не вызывались.

**O2a-UX — `ENABLED` 2026-08-30**: ограниченный пакет делает границы
понятными обычному пользователю tenant-а без изменения модели данных:

- существующее рабочее дерево явно называется «Каталог MAP»;
- защищённые ветки официального дерева Avito подписаны и не предлагают в UI
  переименование или удаление, но сохраняют прежние включение/отключение,
  изображения и наценки;
- текущая вкладка наценок явно названа «Наценки Avito», а Ozon не использует
  эти проценты до отдельного этапа публикации/цен;
- в карточке каждого Ozon-аккаунта доступен поиск по последнему локальному
  снимку дерева с bounded pagination; маршрут не вызывает provider API;
- интерфейс объясняет цепочку «1С/CSV → Каталог MAP → категория площадки» и
  не обещает ещё не реализованную привязку товара к Ozon.

O2a-UX выложен на production SHA
`02ec0cbefa75ba2606d8e555d192ddf763c27e82`; категории и наценки Avito
остались в прежнем runtime.

**O2b — `ENABLED` 2026-08-30**: отдельный physical profile готовит товар к
будущему Ozon preflight без изменения Avito runtime:

- barcode, длина, ширина, высота, вес и НДС хранятся в нейтральных единицах
  (мм, г, проценты) отдельно для валидного значения 1С и fallback MAP;
- эффективное значение всегда выбирает 1С, а MAP используется только при
  отсутствии или ошибке source-значения; импорт не перезаписывает MAP fallback;
- 1С HTTP принимает bounded поля `barcode`, `length_mm`/`length_cm`,
  `width_mm`/`width_cm`, `height_mm`/`height_cm`, `weight_g`/`weight_kg` и
  `vat_rate`; 1С XML использует эквивалентные явные элементы;
- отдельный tenant-scoped API разрешает менять только MAP-половину профиля и
  не вызывает Avito listing sync;
- карточка товара показывает источник каждого значения обычному пользователю,
  вводит размеры в сантиметрах и вес в килограммах и явно сообщает, что блок
  не меняет Avito;
- Ozon offer mapping, provider-specific limits/preflight и любые вызовы
  product/price/stock/archive остаются в O2c/O3.

Локальный gate кандидата O2a-UX: 11 focused и 74 широких
Ozon/provider-neutral/Avito account backend-теста, 46 frontend unit-тестов,
TypeScript, ESLint, production webpack build 21/21, Flake8, baseline/strict
mypy, Django system check, OpenAPI validation и migration drift прошли;
`npm audit --omit=dev` нашёл 0 уязвимостей. Новых migrations нет. Avito
feed/services/tasks, Product/Listing, provider mutations и фоновые задания не
изменялись. Полный CI и production deploy остаются release gate этого пакета.

Локальный gate кандидата O2b: финальные 12 focused-тестов и широкий прогон из
468 product/Avito/provider-neutral тестов прошли; frontend unit —
50/50. TypeScript, ESLint, webpack production build 21/21, полный Flake8,
baseline/strict mypy, Django check, OpenAPI validation, migration drift и
production dependency audit также прошли. Добавлена одна миграция Products;
Ozon provider API и Avito feed runtime не вызываются этим пакетом.

O2b выложен на production SHA
`10a7b9e9ef7b5eb247fb8ef7dadebaa6d88765bd`: migration
`products.0041_product_physical_profile` применена, production topology и
readiness прошли, live-карточка AlfaPro проверена только на чтение.

**O2c1 — `ENABLED` 2026-08-30**: товар подготавливается отдельно для каждого
точного Ozon-аккаунта без создания Avito Listing и без provider mutations:

- `OzonOfferDraft` хранит неизменяемый локальный `offer_id` на связке
  `tenant + product + Ozon account`; переименование аккаунта identity не меняет;
- категория выбирается только из конечных типов последнего account-scoped
  снимка Ozon; путь и ревизия сохраняются отдельно от Каталога MAP и Avito;
- preflight отдельно показывает ошибки аккаунта/склада, категории, физических
  данных, цены, бренда и изображений;
- UI прямо сообщает, что готовность не означает отправку: product import,
  price, stock, archive, Listing и фоновые задачи остаются в O3;
- схема характеристик, справочные значения и окончательный provider-preflight
  намеренно вынесены в отдельный пакет O2c2.

Локальный gate O2c1:

```text
docker compose run --rm django pytest \
  apps/marketplaces/tests/test_ozon_offers.py \
  apps/marketplaces/tests/test_ozon_catalog.py \
  apps/marketplaces/tests/test_ozon_client.py -q
результат: 27 passed

docker compose run --rm django pytest apps/products/tests apps/marketplaces/tests -q
результат: 1323 passed, 2 skipped

npm run test:unit
результат: 50 passed

python3 -m pytest \
  tests/test_runtime_contract.py tests/test_healthchecks.py \
  tests/test_deploy_contract.py -q
результат: 67 passed
```

TypeScript, ESLint, production webpack build 21/21, полный Flake8, baseline
mypy для 718 файлов, strict mypy для 361 production-файла, Django check,
OpenAPI validation, migration apply/drift и оба dependency audit также прошли;
найдено 0 уязвимостей. Пакет содержит одну migration Marketplaces; Avito
feed/services/tasks, Listing и внешние Ozon product/price/stock/archive методы
не изменялись.

O2c1 выложен на production SHA
`9088cf2e09f070b8b51a0bf03e64ed550b0ff069`: migration
`marketplaces.0034_ozon_offer_draft` применена, все 10 production services,
topology и внешний readiness прошли. Read-only проверка карточки AlfaPro
показала отдельный Ozon-блок, точный кабинет и базовые ошибки готовности; новый
черновик или категория во время canary не создавались. Deploy gate возвращён
в `false`.

**O2c2 — `ENABLED` 2026-08-30**: финальная подготовка характеристик остаётся
локальной и account/category-scoped:

- схема характеристик загружается только вручную для выбранного кабинета и
  конечного типа Ozon; автоматических provider reads и фоновых задач нет;
- справочные значения ищутся явной кнопкой через read-only endpoint Ozon с
  точными `description_category_id`, `type_id` и `attribute_id`;
- результат поиска связан с точной ревизией схемы; произвольный или устаревший
  dictionary ID из браузера не сохраняется, а текст значения берётся из
  нормализованного ответа Ozon;
- смена кабинета или категории очищает локальные результаты поиска, а ключи UI
  включают account/category/type/attribute и не смешивают данные кабинетов;
- preflight требует актуальную схему и каждую обязательную характеристику;
  optional-поля остаются рекомендациями интерфейса;
- Ozon-характеристики и их снимки хранятся отдельно от Каталога MAP, дерева и
  наценок Avito; Listing, Avito feed/services/tasks и provider mutations не
  изменяются.

Локальный gate O2c2: 29 focused Ozon-тестов и широкий прогон Products +
Marketplaces — 1325 passed, 2 skipped; frontend unit — 52/52; runtime/deploy
contracts — 67/67. TypeScript, ESLint, production webpack build 21/21, полный
Flake8, baseline mypy для 719 файлов, strict mypy для 361 production-файла,
Django check, OpenAPI validation, migration drift и чистое применение всей
цепочки до `marketplaces.0035_ozon_offer_attributes` прошли. Production npm
audit нашёл 0 уязвимостей. Пакет содержит одну migration Marketplaces.

O2c2 выложен на production SHA
`a68ad3f83ccf565b3296144a13ddca0ebbdd25a9`: migration
`marketplaces.0035_ozon_offer_attributes` применена, все 10 production
services, topology и внешний readiness прошли. Read-only AlfaPro
canary подтвердил account-scoped подготовку без provider mutations;
deploy gate возвращён в `false`.

**O2c3 — `CODE_READY` 2026-08-31**: общее обогащение товара и подготовка
Ozon объединены в один понятный tenant-facing сценарий без изменения Avito:

- после parser/AI enrichment MAP ставит отдельное безопасное автозаполнение
  для каждого активного Ozon-кабинета точного tenant-а;
- бренд, артикул производителя, стабильное название модели, выбранный тип и
  подтверждённый barcode переносятся только из известных фактов товара;
- dictionary-поля сохраняются автоматически только при одном точном совпадении
  в account/category/schema-scoped справочнике Ozon;
- ТН ВЭД, маркировка, категория без однозначного mapping и неизвестные
  обязательные поля не придумываются: tenant получает рекомендацию и заполняет
  их вручную;
- ручное значение получает provenance «Проверено тенантом» и последующее
  обогащение его не перезаписывает;
- один редактор Ozon используется и в карточке товара для первичной модерации,
  и в provider-aware drawer раздела «Листинги» для повторного preflight;
- внешние product/price/stock/archive методы Ozon, `Listing`, Avito feed,
  Avito category tree и Avito-наценки пакет не меняет.

Локальный gate O2c3: Ozon offer — 12/12; parser/AI/Avito listing review —
195/195; Products + Marketplaces — 1356 passed, 2 skipped; frontend unit —
63/63; runtime/deploy contracts — 67/67; production build — 21/21 страниц.
TypeScript, ESLint, полный Flake8, baseline mypy для 726 файлов, production
mypy для 364 файлов, Django check, OpenAPI validation и migration drift
прошли. Пакет содержит одну migration Marketplaces
`0037_ozon_offer_autofill`; полный CI и deploy остаются release gate.

## O3 — публикация и reconciliation

O3 выполняется двумя последовательными пакетами:

- **O3a — provider-aware listing workspace (`ENABLED` 2026-08-30)**:
  общие данные и медиа остаются у товара, а готовность и действия
  разделены по точному Avito/Ozon-аккаунту; Ozon остаётся
  локальной подготовкой без кнопки provider publication;
- **O3b — durable Ozon publication/reconciliation**: create/update/archive,
  idempotency, очередь, unknown-result handling и provider-status polling.

O3a добавляет понятный tenant-facing drawer «Каналы публикации»:

- цель выбирается и сохраняется в URL по точному marketplace account;
- у каждого Avito-кабинета показана готовность и открывается
  прежний Avito drawer; создание черновика идёт через текущий
  stable path и не публикует его автоматически;
- у каждого Ozon-кабинета показан его локальный `OzonOfferDraft`, ошибки
  и характеристики; данные Ozon не смешиваются с деревом,
  наценками и фидом Avito;
- в пакете нет backend-изменений, migrations, background tasks или
  новых Ozon provider methods.

Локальный gate O3a: frontend unit — 56/56, TypeScript, ESLint,
production webpack build 21/21 и production dependency audit с 0 уязвимостей;
Products + Marketplaces regression — 1325 passed, 2 skipped; runtime/deploy
contracts — 67/67. `git diff --check` чистый.

O3a выложен на production SHA
`63c406efd231e21fa0cf349200dfc58ca6ff7967`: все 10 production services,
topology и внешний readiness прошли. Read-only AlfaPro canary подтвердил
раздельные Avito/Ozon-кабинеты и отдельную Ozon-проекцию; черновики и provider
mutations не запускались. Deploy gate возвращён в `false`.

**O3a-UX — 2026-08-30** устраняет промежуточный экран,
который дублировал выбор целей и смешивал подготовку товара с публикацией:

- карточка товара содержит только общие факты, обогащение, медиа и ручную
  модерацию; упаковка и налог подписаны provider-neutral;
- категории, обязательные поля, кабинет и действия конкретного маркетплейса
  перенесены в раздел `Листинги`;
- один видимый drawer переключается между точными Avito/Ozon-кабинетами;
  существующий Avito-листинг открывается сразу в прежней полной Avito-форме,
  без промежуточной summary-карточки;
- Avito publication/feed services, backend, migrations и Ozon provider
  mutations в пакет не входят.

Локальный gate O3a-UX: frontend unit — 58/58, TypeScript, ESLint и production
webpack build 21/21; Products + Marketplaces — 1333 passed, 2 skipped; полный
backend — 2841 passed, 3 skipped; Flake8, baseline mypy для 719 файлов, strict
mypy для 361 production-файла, OpenAPI validation и migration drift прошли.

Результат O3b: create/update/archive Ozon offer с устойчивым
локальным статусом.

- durable intent и idempotency для provider mutations;
- account-scoped очередь и rate limiting;
- неизвестный результат не повторяется вслепую;
- polling/reconciliation фактического provider status;
- tenant-visible ошибки, повторная проверка и безопасный retry;
- audit evidence без секретов и чужих tenant identifiers.

Gate: fault tests для timeout/429/5xx/partial result, полный backend/frontend
gate, выключенный deploy и один account-scoped production canary.

## E1 — безопасность обогащения для мультитематического каталога

E1a отделяет найденную применяемость от подтверждённой и не использует
название товара как жёсткий фильтр:

- Tachka читает применяемость только из профильной секции карточки или из
  размеченного блока `Подходит для следующих модификаций` точного Product
  JSON-LD; общий текст страницы и рекомендации не сканируются;
- Rossko читает только профильную вкладку `applicability` конкретной карточки;
- Euroauto читает профильный блок карточки либо `title + content` точного
  результата по артикулу; full-page `raw_content` не участвует в извлечении
  или ранжировании применяемости;
- все найденные parser-fitments остаются tenant-scoped доказательствами до
  ручного `Одобрить`/`Отклонить`; в AI-контекст и денормализованную
  применяемость попадают только одобренные варианты;
- между тенантами распространяется только копия с provenance `human_review`;
  старые pending-записи без `needs_review` также требуют решения человека;
- у тенанта с несколькими доменами конкретная неавтомобильная категория
  блокирует Tachka/Rossko/Euroauto, но обычная AI-генерация для одежды и других
  товаров остаётся доступной.

E1b отдельно добавит отметку устаревшего AI-текста после решения оператора и
предложения физических параметров MAP с provenance; автоматическое удаление
или регенерация ранее созданного текста в E1a не выполняются.

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
