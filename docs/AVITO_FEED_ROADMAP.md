# Roadmap надёжной отправки фидов Avito

Обновлено: 2026-08-27.

Текущий статус: [`AVITO_FEED_STATUS.md`](AVITO_FEED_STATUS.md).
Правила выполнения:
[`ENGINEERING_EXECUTION_RULES.md`](ENGINEERING_EXECUTION_RULES.md).
Точная карта файлов и тестов:
[`AVITO_FEED_CHANGESET_MANIFEST.md`](AVITO_FEED_CHANGESET_MANIFEST.md).

## Цель

Не потерять изменение товара или объявления, безопасно переживать сбои worker
и внешнего API и обслуживать много tenant/account без публичного хранения XML.
Эта цель закрыта P0–P6. Автоматическое удаление старых файлов и DB cleanup
остаются отдельной будущей целью P7.

## Текущая фаза: наблюдение P6 fleet-default; P7 заморожен

P0 и P1 завершены. Полный P1 observability, включая code-owned Sentry Cron
dead-man, работает в production commit `c2bc2eb`; check-in, test-fire и alerts
проверены. P2a nullable schema expansion `0020`–`0021` выложен в production
commit `decd480` без runtime-логики; schema, health, backup и monitor gates
зелёные. P2b1 с чистой lifecycle-логикой, индексом и ручным backfill выложен в
production commit `de0d202` с режимом `legacy`; schema, health, monitor и
десятиминутное наблюдение зелёные. P2b2 с runtime fencing выложен в production
commit `0ef04de` без смены режима `legacy`; PR/main CI, manual monitor, health и
десятиминутное наблюдение зелёные. P2b2 закрыт как legacy-only release.
P2c follow-up с provider truth, временем последней Avito-проверки и
tenant notices выложен в production commit `1f05367` без изменения
feed-режима. Первый плановый цикл повторно подтвердил 10 active
объявлений и доставил два разных Telegram notice для порога 14 дней.
P3–P5 были сначала выложены выключенными и затем последовательно активированы.
P6 private artifacts, recovery и account-scoped canary прошли PR `#249`–`#255`.
PR `#256` завершил fleet-default release на production SHA `0762ab5`: run
`durable`, ingress/lifecycle `dual_write`, artifacts `active`, storage
`stable_bridge`, allowlist пустой. Любой будущий успешно подключённый Avito
account получает stable endpoint и durable/private delivery автоматически.

Текущий gate — observation без изменения кода P7:

- terminal результат реального Avito upload `587751397` получен: durable run
  завершён как `succeeded`, blocking report пуст, duplicate PUT/POST и
  unresolved/uncertain evidence не обнаружены;
- следить за duplicate POST/PUT, uncertain runs, unresolved attempts, 5xx,
  queue lag и restart;
- зафиксировать retention/restore policy до разрешения любого object delete;
- отдельно согласовать P7. Обычные продуктовые задачи вне P7 не блокируются.

## Логическое разделение текущей большой правки

Пакеты идут строго по порядку. Каждый становится отдельным commit/PR и может
быть проверен независимо от последующих.

### P0. Документация и инвентаризация — завершён

Содержимое:

- этот статус, roadmap и правила;
- список файлов каждого следующего пакета;
- удаление случайных изменений из состава релиза;
- никаких изменений runtime-кода.

Проверка: ссылки документации, `git diff --check`, отсутствие новых миграций.

Deploy: отсутствует.

### P1. Общая production-наблюдаемость — завершён

Содержимое:

- telemetry/queue monitoring/Sentry scrubbing;
- production host checks и связанные тесты;
- без изменения логики фидов.

Проверки:

- тесты observability, Sentry и production host contract;
- полный backend test;
- проверка, что feed-флаги остались `legacy/legacy/disabled`.

Deploy: foundation и Cron dead-man работают в production на commit `c2bc2eb`.
Новые фиды не включались.

### P2. Статусы объявлений Avito

P2 разделён на два последовательных release, потому что общий пакет содержит
три миграции, а execution rules разрешают не более двух.

#### P2a. Additive schema expansion — завершён

Содержимое:

- migration `0020` с девятью nullable полями;
- migration `0021` с concurrent partial listing index;
- model state и schema/upgrade/rollback tests;
- без lifecycle-кода, backfill и scheduler.

Проверки:

- PostgreSQL clean/upgrade/rollback и catalog contracts;
- весь Marketplace suite и полный backend test;
- migration drift, flake8, mypy и OpenAPI.

Deploy: production commit `decd480`, только схема. Старый код продолжает
работать, новые поля остаются `NULL`, ни один новый worker или режим не
включён. Production monitor и десятиминутное наблюдение зелёные.

#### P2b1. Lifecycle, индекс и ручной backfill — завершён

Содержимое:

- migration `0022` с account due index;
- единые правила смены статусов;
- безопасный backfill статусов;
- явный production-режим `legacy`;
- без нового планировщика и без private storage.

Проверки:

- schema/lifecycle/backfill/settings tests;
- обновление копии существующей базы;
- полный backend test.

Deploy: status mode остаётся `legacy`, новый scheduler выключен. Schema/index,
manual monitor, health и десятиминутное наблюдение подтверждены на production
commit `de0d202`. Backfill apply не запускался.

#### P2b2. Runtime fencing/dual-write — завершён в legacy

Содержимое:

- только lifecycle-hunks существующих marketplace tasks/services;
- fencing устаревшего ответа внешнего API;
- dual-write служебных lifecycle-полей при сохранении канонического legacy
  статуса объявления;
- без scheduler activation, private storage и feed-run логики.

Проверки: status-fencing tests, весь Marketplace suite и полный backend test.
Локальный gate закрыт 2026-08-21: 15 status-fencing, 292 Marketplace и 1 937
backend-тестов прошли; migration drift, OpenAPI, flake8 и mypy зелёные.
PR `#232`, PR CI `32491344632` и main CI `32493682105` зелёные. Production
commit `0ef04de` работает в режиме `legacy`; lifecycle scheduler отсутствует,
manual monitor `32496429816` и десятиминутное наблюдение зелёные.

Bounded read-only canary по всем десяти ID не подтвердил ошибку счётчика:
Avito вернул `active` для каждого, а account-wide active list содержит все
десять и ещё четыре объявления того же API-аккаунта. Credentials соответствуют
настроенному account. Поэтому искусственный переход `active → archived` не
делается.

#### P2c. Понятный tenant-facing статус и срок размещения — production legacy-only

Содержимое:

- дашборд явно называет число локальным счётчиком MAP;
- список и карточка показывают точное время последней проверки через Avito;
- существующая проверка использует `finish_time` из ответа Avito и отправляет
  уведомления за 14, 7, 3, 1 и 0 дней;
- event key и cache coalescing не допускают повторной доставки одного порога;
- срок не вычисляется как фиксированные 30 дней и не сохраняется в новой
  колонке;
- без миграций, новых scheduler/queue/settings и без изменения canonical
  статуса или feed-режима.

Проверки: 25 status-fencing, 303 Marketplace и 1 948 backend-тестов прошли;
frontend typecheck, ESLint, 25 unit tests и production build зелёные. Migration
drift, OpenAPI, flake8 и оба mypy gate зелёные. PR `#234`, PR CI
`32507225396` и main CI `32509442153` зелёные. Production commit
`1f05367` работает в режиме `legacy`; exact topology, readiness,
backup, manual monitor `32512208535` и десятиминутное наблюдение зелёные.

### P3. Надёжная запись задания на отправку — завершён и включён

Содержимое:

- миграции `0023`–`0024`;
- запуск фида, его состояние и ручное разрешение неизвестного результата;
- восстановление потерянного задания;
- лимит 10 000 объявлений на один аккаунт.

История запусков в P3 не очищается автоматически. Retention-удаление,
отвязывание записей и GC остаются замороженным пакетом P7; до его отдельной
активации строки feed run сохраняются.

Проверки:

- feed run/workflow/recovery/payload-limit tests;
- сбой брокера, остановка worker и повторная доставка;
- полный backend test.

История deploy: PR `#244`, production commit `f1881f1`; schema `0023`–`0024`
сначала была выложена выключенной. После P5/P6 gates текущий production run
mode — `durable`.

### P4. Стабильная ссылка и профиль Autoload — завершён и включён

Содержимое:

- миграция `0025`;
- стабильная ссылка;
- account-scoped inspect/migrate/reconcile commands;
- защита токена и настроек аккаунта.

Проверки:

- endpoint/profile/route tests;
- реальная тестовая проверка Avito query string и HTTP 307;
- rehearsal без изменения production-профиля.

Первичный deploy выполнялся только с выключенным
`MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=false` и
`MARKETPLACE_FEED_STORAGE_MODE=legacy_public`.

Локальный gate: 105 P4-тестов и полный backend (`2198 passed, 1 skipped`)
зелёные; clean/upgrade/rollback/reapply PostgreSQL `0025`, migration drift,
flake8, frontend ESLint/typecheck и `git diff --check` прошли. PR `#245`
выпущен в production commit `9061ebb`; legacy-режим тогда не изменился.
Сейчас stable endpoint и profile protection используются fleet onboarding.
Флаг profile migration остаётся `false`, потому что массовый sweep не нужен.

### P5. Учёт изменений — завершён и включён

Содержимое:

- миграции `0026`–`0028` только для cursor/schema foundation;
- атомарные примитивы feed intent и exact-revision dark worker;
- минутный scanner: в production `legacy` он строго инертен;
- terminal-dispatch recovery для ещё не активированного durable leaf.

Транзакционные writer-хуки, provider-result reconciliation и замена legacy
flush-coordinator не входят в этот release. Их нельзя выпускать частично:
они требуют отдельного P5 activation package, fault-test и разрешения после
наблюдения additive schema.

Проверки:

- intent/schema/dispatch/recovery и production-settings tests;
- миграции на чистой и обновляемой PostgreSQL;
- полный backend test;
- staging fault test.

История deploy foundation: PR `#246`, production commit `2e9958c`, первоначально
только `legacy`. Текущий production ingress — `dual_write`, run — `durable`.

### P5 activation. Writer fencing и надёжная legacy delivery

Содержимое:

- атомарные product/listing/image/category/address/account writer-хуки;
- account-first lock order и exact-generation fencing;
- provider-result и page-bounded feed-report reconciliation;
- durable desired cursor и scanner-repair при сбое публикации legacy flush;
- fail-closed hold после неоднозначного provider POST;
- закрытие прямых feed-visible writer/delete путей в Django Admin.

Границы:

- ровно 20 production-файлов, новых миграций и production settings нет;
- P6/P7, private storage/serving, cleanup/GC и `0039` не входят;
- initial production deploy остаётся
  `legacy/legacy/disabled/false/legacy_public`;
- разрешённый observation меняет только ingress и lifecycle на `dual_write`,
  оставляя run `legacy`, а rollback одновременно возвращает оба режима.

Локальный gate: 208 focused tests, полный backend (`2356 passed, 1 skipped`),
flake8, mypy для 665 и strict mypy для 338 source files, migration drift и
`git diff --check` прошли.

Release gate закрыт: PR `#247`, production `9c23a6b`, все десять контейнеров
healthy с restart count `0`, readiness/topology/Celery зелёные, scanner в
legacy инертен. P5 `dual_write` observation отдельно разрешён 2026-08-25.
Минимальный production settings gate локально прошёл 150 focused tests, полный
backend (`2357 passed, 1 skipped`), оба mypy, flake8 и migration drift. Этот
этап завершён; текущие значения приведены в `AVITO_FEED_STATUS.md`.

### P6. Приватные файлы и fleet onboarding — завершён и включён

Содержимое:

- две свёрнутые миграции `0029`–`0030` поверх production `0028` (artifact
  schema и все P6 PostgreSQL guards);
- versioned private bucket;
- запись и проверка точной версии XML;
- ручная сверка неизвестного результата.

Release gate:

- отдельные credentials, IAM/KMS и presigner, включая сверку folder owner
  через `GetBucketAcl`;
- реальный bucket canary;
- тест 10 000 объявлений с памятью, диском и временем;
- утверждённый способ восстановления.

P6 отдельно разрешён владельцем продукта 2026-08-25. Первичный deploy был
выключенным, затем выполнены bounded canary/recovery account `4` и общий
fleet-default rollout. P7, retention delete, GC, удаление файлов и `0039` в
пакет не входят.

Исторический локальный P6 gate закрыт: полный backend дал
`2592 passed, 3 skipped`,
flake8, оба mypy gate, migration drift и OpenAPI зелёные; свежая PostgreSQL
база прошла `0029`–`0030` и rollback/reapply `0030 → 0028 → 0030`. Основной
release и cloud preflight завершены. Первый canary account 4 оставил
fail-closed `put_pending`; audited recovery без слепого повторного PUT успешно
разрешил этот случай.

Recovery и account `4` cutover завершены в PR `#253`–`#255`. PR `#256`
перевёл admission с exact-one allowlist на fleet default:
`active/stable_bridge`, пустой allowlist и `run=durable`. Создание аккаунта
синхронно резервирует endpoint, фоновая задача сохраняет настройки клиента и
регистрирует stable URL, а публикация ждёт подтверждения вместо legacy
fallback. Неоднозначный POST сверяется только GET-запросами и не повторяется
вслепую. Production release/health evidence записано в текущем статусе. P7,
object delete, GC и `0039` не меняются.

### P7. Удаление старых файлов и DB-защита — backlog

Сюда относятся `0036`–`0039`, cleanup служебных исключений, автоматическое
удаление XML и перевод новой связи заданий в обязательную.

Этот пакет не начинается, пока:

- не завершён согласованный fleet observation;
- terminal outcome реального Avito upload не подтверждён либо не оформлен
  incident; для upload `587751397` этот gate закрыт состоянием `succeeded`;
- dry-run production backfill не показал реальные данные;
- не утверждена политика хранения и восстановления;
- владелец продукта отдельно не разрешил этап.

Bounded cleanup-команда имеет статус `CODE_READY`, но остаётся вне текущих
релизных пакетов до PostgreSQL/full-suite проверки и production dry-run. Она не
разрешает начинать P7 целиком. Cleanup/backfill и auto-applied `0039` должны
выходить разными release: второй допустим только после фактической очистки,
полного повторного backfill и подтверждённого fleet/broker drain.

## Обязательная проверка каждого пакета

1. Узкие тесты пакета.
2. `makemigrations --check --dry-run`.
3. Миграции на чистой PostgreSQL.
4. Миграции копии существующей схемы.
5. Полный backend `pytest`.
6. `flake8` и `git diff --check`.
7. Проверка старого legacy-сценария.
8. Отдельный короткий отчёт с фактическими командами и результатами.

Если любой пункт не пройден, пакет не объединяется со следующим.

## Порядок выкладки

```text
P0 docs/inventory
  → P1 observability
  → P2 listing lifecycle
  → P3 durable job foundation (off)
  → P4 stable endpoint/profile (off)
  → P5 dual-write observation
  → P6 private storage (off deploy → canary → account 4 → fleet default)
  → P6 fleet observation (текущий этап)
  → отдельное решение о P7 cleanup/GC/0039
```

Текущий минимальный admission rollback одной согласованной сменой возвращает
`run=legacy` и exact allowlist account `4`, не удаляя artifact, endpoint,
VersionId и evidence. Более глубокий откат artifact выполняется только по P6
runbook. Полный аварийный rollback предыдущих пакетов не является частью
обычного fleet rollback. Последующий пакет не используется как условие отката
предыдущего.
