# Roadmap надёжной отправки фидов Avito

Обновлено: 2026-08-21.

Текущий статус: [`AVITO_FEED_STATUS.md`](AVITO_FEED_STATUS.md).
Правила выполнения:
[`ENGINEERING_EXECUTION_RULES.md`](ENGINEERING_EXECUTION_RULES.md).
Точная карта файлов и тестов:
[`AVITO_FEED_CHANGESET_MANIFEST.md`](AVITO_FEED_CHANGESET_MANIFEST.md).

## Цель

Первый результат — не потерять изменение товара или объявления и при этом не
сломать существующую отправку. Приватные файлы, их автоматическое удаление и
полностью новая отправка — отдельные будущие результаты.

## Текущая фаза: P2c tenant visibility выложен в production

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
P3 и последующие пакеты не начинаются без нового отдельного решения
пользователя.

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

### P3. Надёжная запись задания на отправку

Содержимое:

- миграции `0023`–`0024`;
- запуск фида, его состояние и ручное разрешение неизвестного результата;
- восстановление потерянного задания;
- лимит 10 000 объявлений на один аккаунт.

Проверки:

- feed run/workflow/recovery/payload-limit tests;
- сбой брокера, остановка worker и повторная доставка;
- полный backend test.

Deploy: код выкладывается выключенным. Legacy остаётся единственным владельцем
отправки.

### P4. Стабильная ссылка и профиль Autoload

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

### P5. Учёт изменений и восстановление legacy-отправки

Содержимое:

- миграции `0026`–`0031`;
- запись намерения обновить фид вместе с изменением товара/объявления;
- восстановление потерянного legacy-задания;
- связанные writer/retention/admin guards.

Проверки:

- intent/writer/provider-result/legacy-repair tests;
- миграции на чистой и обновляемой PostgreSQL;
- полный backend test;
- staging fault test.

Deploy: сначала `legacy`; затем отдельный наблюдаемый `dual_write` canary.
Новая система не отправляет файлы в Avito.

### P6. Приватные файлы — отдельный экспериментальный проект

Содержимое:

- миграции `0032`–`0035`;
- versioned private bucket;
- запись и проверка точной версии XML;
- ручная сверка неизвестного результата.

До начала обязательны:

- решение, что private storage действительно нужен сейчас;
- отдельные credentials, IAM/KMS и presigner;
- реальный bucket canary;
- тест 10 000 объявлений с памятью, диском и временем;
- утверждённый способ восстановления.

Deploy: только как выключенный эксперимент. Production activation не входит в
этот пакет.

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
  → отдельное решение о P6 private storage
  → отдельное решение о P7 cleanup/GC/0039
```

В любой момент rollback возвращает режимы к
`legacy/legacy/disabled/legacy_public`. Последующий пакет не используется как
условие отката предыдущего.
