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
- **O2c — offer mapping и preflight UI**: stable offer identity, выбор leaf
  type, dictionary values и field-level ошибки обязательных полей.

O2a и O2a-UI не используют Avito feed/services/tasks, не меняют Listing/Product
и не вызывают product/price/stock/archive методы Ozon. Реальный catalog refresh
остаётся отдельным ручным read-only действием точного tenant/account.

O2a foundation и O2a-UI выложены на production 2026-08-30. Read-only canary
AlfaPro сохранил отдельный account-scoped снимок: 9 796 узлов, 8 876 активных
типов, revision `0985c3c51042`; после полной перезагрузки страницы выбранные
Ozon и аккаунт сохранились. Product/price/stock/archive методы не вызывались.

**O2a-UX — `CODE_READY` 2026-08-30**: текущий ограниченный пакет делает границы
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

**O2b — `CODE_READY` 2026-08-30**: отдельный physical profile готовит товар к
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
