# Roadmap надёжной отправки фидов Avito

Обновлено: 2026-08-27.

Текущий статус: [`AVITO_FEED_STATUS.md`](AVITO_FEED_STATUS.md).
Правила выполнения:
[`ENGINEERING_EXECUTION_RULES.md`](ENGINEERING_EXECUTION_RULES.md).
Точная карта файлов и тестов:
[`AVITO_FEED_CHANGESET_MANIFEST.md`](AVITO_FEED_CHANGESET_MANIFEST.md).

## Цель

Первый результат — не потерять изменение товара или объявления и при этом не
сломать существующую отправку. Приватные файлы, их автоматическое удаление и
полностью новая отправка — отдельные будущие результаты.

## Текущая фаза: P6 fleet-default onboarding и private delivery

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
P3 merged через PR `#244`, выложен в production commit `f1881f1` и остаётся
выключенным (`MARKETPLACE_FEED_RUN_MODE=legacy`). P4 merged через PR `#245` и
выложен выключенным в production commit `9061ebb`. P5 schema/intent foundation
merged через PR `#246` и выложен в production commit `2e9958c` в legacy-only
режиме. Отдельный P5 activation package с writer fencing, reconciliation и
legacy delivery repair merged через PR `#247` и выложен exact commit `9c23a6b`.
PR/push-main CI, encrypted backup, health/topology, Celery ping, scanner cycles
и отсутствие свежих critical/500 ошибок подтверждены. P5 `dual_write`
observation завершён. P6 private artifact package и bounded follow-up/recovery
merged через PR `#249`–`#254`. PR `#255` завершил постоянный private cutover
единственного реального Autoload account `4`; production exact SHA перед
fleet-release — `139ed48`. Реальный GET Avito подтвердил загрузку поколения.
Владелец продукта отдельно разрешил один P6 fleet-default PR: любой будущий
успешно подключённый аккаунт автоматически получает stable endpoint,
регистрацию URL и durable/private delivery без ручного allowlist. P7 остаётся
заморожен.

Готово только когда:

- состав каждого пакета зафиксирован;
- случайные и личные файлы исключены;
- каждый пакет имеет собственные тесты и порядок выкладки;
- текущий legacy-режим проходит полный backend test;
- миграции проходят на чистой PostgreSQL;
- ни один новый feed-флаг не включён.

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

### P3. Надёжная запись задания на отправку — завершён, выключен

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

Deploy: PR `#244`, production commit `f1881f1`; schema `0023`–`0024`
применена, код выключен. Legacy остаётся единственным владельцем отправки.

### P4. Стабильная ссылка и профиль Autoload — локально проверен

Содержимое:

- миграция `0025`;
- стабильная ссылка;
- account-scoped inspect/migrate/reconcile commands;
- защита токена и настроек аккаунта.

Проверки:

- endpoint/profile/route tests;
- реальная тестовая проверка Avito query string и HTTP 307;
- rehearsal без изменения production-профиля.

Deploy: только с выключенным
`MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=false` и
`MARKETPLACE_FEED_STORAGE_MODE=legacy_public`. Включение — отдельное решение.

Локальный gate: 105 P4-тестов и полный backend (`2198 passed, 1 skipped`)
зелёные; clean/upgrade/rollback/reapply PostgreSQL `0025`, migration drift,
flake8, frontend ESLint/typecheck и `git diff --check` прошли. PR `#245`
выпущен в production commit `9061ebb`; legacy-режим не изменён.

### P5. Учёт изменений — безопасный foundation

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

Deploy foundation: PR `#246`, production commit `2e9958c`, только `legacy`;
новая система не отправляет файлы в Avito и не меняет legacy flush.

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
backend (`2357 passed, 1 skipped`), оба mypy, flake8 и migration drift; до его
deploy production environment остаётся legacy.

### P6. Приватные файлы — активированный экспериментальный проект

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

P6 отдельно разрешён владельцем продукта 2026-08-25 и собирается одним PR.
Первичный deploy выполняется только выключенным. После успешного release gate
разрешён ручной canary ровно одного явно выбранного Avito-аккаунта с атомарным
переключением и rollback без удаления объекта. Широкая production-активация,
P7, retention delete, GC, удаление файлов и `0039` в пакет не входят.

Локальный P6 gate закрыт: полный backend дал `2592 passed, 3 skipped`,
flake8, оба mypy gate, migration drift и OpenAPI зелёные; свежая PostgreSQL
база прошла `0029`–`0030` и rollback/reapply `0030 → 0028 → 0030`. Основной
release и cloud preflight завершены. Первый canary account 4 не прошёл PUT:
durable attempt осталась `put_pending`, endpoint продолжил legacy serving и
runtime возвращён в `disabled/stable_bridge`. Следующий gate — один P6 recovery
PR, audited exact-version reconciliation, safe resume новой immutable attempt,
проверка canary и точный rollback без удаления объекта.

Recovery и account `4` cutover завершены в PR `#253`–`#255`. Следующий
разрешённый gate переводит admission с exact-one allowlist на fleet default:
`active/stable_bridge`, пустой allowlist и `run=durable`. Создание аккаунта
синхронно резервирует endpoint, фоновая задача сохраняет настройки клиента и
регистрирует stable URL, а публикация ждёт подтверждения вместо legacy
fallback. Неоднозначный POST сверяется только GET-запросами и не повторяется
вслепую. P7, object delete, GC и `0039` не меняются.

### P7. Удаление старых файлов и DB-защита — backlog

Сюда относятся `0036`–`0039`, cleanup служебных исключений, автоматическое
удаление XML и перевод новой связи заданий в обязательную.

Этот пакет не начинается, пока:

- P1–P6 не разделены и проверены;
- новая система не прошла staging canary;
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
  → отдельное решение о P7 cleanup/GC/0039
```

Текущий безопасный P6 rollback возвращает artifact/storage в
`disabled/stable_bridge`, сохраняя отдельно проверенный P5
`legacy/dual_write/dual_write`. Полный аварийный rollback предыдущих пакетов
может дополнительно вернуть ingress/lifecycle в `legacy`, но не является
частью account 4 recovery. Последующий пакет не используется как условие
отката предыдущего.
