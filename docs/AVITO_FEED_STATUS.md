# Текущее состояние работ по фидам Avito

Обновлено: 2026-08-25.

Исходный WIP сохранён как локальный `not-for-merge` snapshot:

- branch `codex/wip-not-for-merge-avito-scaling-20260820`;
- commit `8ae8e265656dcae00a96111ac6477bb1d05e7e8f`;
- base `7415ccca0ae54fccc9cb389704fa8e183feea213`;
- 65 изменённых и 104 добавленных файла, всего 169;
- diff snapshot: 61 114 добавлений и 938 удалений.

`.claude/settings.local.json` намеренно исключён и не входит ни в один пакет.
Snapshot хранит смешанный незавершённый WIP, не предназначен для merge или
release и не означает, что его код присутствует в P0-ветке.

P0 сохранён commit `646ee62d3113042fb0d283c4257e65d5611caa40`.
P1 observability был выделен по hunks, проверен, merged через PR `#225` и
дополнен Sentry Cron dead-man collector через PR `#226`. Полный P1 работает в
production на commit `c2bc2eb102c5caa7610cd15e2a8dfac8193e0a34`; feed-код и
feed-флаги P1 не менял. P2 завершён последовательными legacy-only release.
P3 merged через PR `#244` и выложен выключенным на production commit
`f1881f123a0bbd4fc2534ba746eaff19af8f851b`. Активный P4 физически выделен,
локально проверен и готовится одним PR; его runtime activation в этот release
не входит.

Этот файл — единственный источник правды о текущей стадии работ. Roadmap
находится в [`AVITO_FEED_ROADMAP.md`](AVITO_FEED_ROADMAP.md), а обязательные
правила выполнения — в
[`ENGINEERING_EXECUTION_RULES.md`](ENGINEERING_EXECUTION_RULES.md).
Точная карта разделения файлов и тестов:
[`AVITO_FEED_CHANGESET_MANIFEST.md`](AVITO_FEED_CHANGESET_MANIFEST.md).

## Решение: механизмы после активного P4 заморожены

До отдельного решения владельца продукта запрещено выходить за границы P4:

- добавлять новые механизмы фидов, миграции и режимы настроек;
- продолжать `0039`, автоматическое удаление старых файлов и private serving;
- подключать новую систему к рабочему Celery worker или Avito;
- включать в production новые feed-флаги.

Разрешено только:

- разделить текущий набор изменений на независимые пакеты;
- запускать проверки и исправлять только обнаруженные ими ошибки;
- сохранять bounded cleanup как `CODE_READY`; менять его дальше можно только
  для ошибки, подтверждённой тестом, без добавления `0039`;
- упрощать документацию;
- удалять из пакета случайно попавшие или не относящиеся к нему изменения.

## Почему введена заморозка

Snapshot меняет 169 файлов и добавляет больше 61 тысячи строк. Такой объём и
смешение P0–P7 нельзя считать одним проверяемым релизом.

Работа ушла дальше необходимой цели: до цельной проверки уже написанного были
начаты будущие механизмы приватных файлов, их удаления и дополнительной защиты
служебных заданий.

## Что сейчас работает в production

Рабочая система продолжает использовать старую отправку фидов. Обязательные
значения после появления соответствующих runtime-переключателей:

```text
MARKETPLACE_FEED_RUN_MODE=legacy
MARKETPLACE_FEED_INGRESS_MODE=legacy
MARKETPLACE_FEED_ARTIFACT_MODE=disabled
MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=false
MARKETPLACE_FEED_STORAGE_MODE=legacy_public
```

Текущий production commit P3 определяет `MARKETPLACE_FEED_RUN_MODE=legacy` и
оставляет lifecycle в `legacy`; durable owner не активирован. P4 при deploy
добавит только явные безопасные значения
`MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=false`,
`MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS=3600` и
`MARKETPLACE_FEED_STORAGE_MODE=legacy_public`. HMAC keyring и stable URL не
нужны, пока stable bridge выключен.

## Фактическая стадия snapshot

Таблица ниже описывает сохранённый WIP snapshot, а не runtime-код P0-ветки.

| Часть | Состояние | Можно включать? |
|---|---|---|
| Текущая старая отправка | Работает сейчас | Да, это текущий режим |
| Учёт изменений товаров и объявлений | Код написан, общая проверка не завершена | Нет |
| Надёжные задания на повторную отправку | Код написан, общая проверка не завершена | Нет |
| Стабильная ссылка на фид | Код написан, реальная проверка с Avito не выполнена | Нет |
| Приватные версионированные XML-файлы | Изолированный экспериментальный код | Нет |
| Безопасное удаление старых XML-файлов | Не реализовано; есть только неисполняемая схема-кандидат | Нет |
| Нормализованная связь задания с запуском фида | Экспериментальный код `0038` | Нет |
| Cleanup служебных исключений | `CODE_READY`: bounded dry-run/apply, статика пройдена; PostgreSQL gate не выполнен | Нет |
| Защита базы `0039` | Не начата | Нет |
| Hardening удаления MarketplaceAccount | Частично записан в `models.py` и `retention.py`, тестирование не завершено | Нет |

## Что действительно проверено

Для отдельных частей ранее запускались узкие тесты и статические проверки.
Tracked working-tree diff ранее проходил `py_compile`, `flake8` и
`git diff --check`, но untracked-файлы в последнюю команду не входили.
Проверка полного snapshot `git diff HEAD^ HEAD --check` затем обнаружила по
одной лишней пустой строке в конце `AGENTS.md` и
`ENGINEERING_EXECUTION_RULES.md`. Snapshot сохранён без исправления для
точности; P0 содержит исправленные версии документов. Эти проверки не заменяют
проверку всего проекта.

Для документационной P0-ветки от чистого `origin/main` 2026-08-20 выполнен
полный baseline gate:

```text
docker compose up -d --wait --wait-timeout 60 db redis
результат: exit 0, PostgreSQL и Redis healthy

docker compose run --rm django python manage.py migrate --noinput
результат: exit 0, все существующие миграции применены на чистой PostgreSQL

docker compose run --rm django python manage.py makemigrations --check --dry-run
результат: exit 0, No changes detected

docker compose run --rm django flake8 .
результат: exit 0

docker compose run --rm django pytest --cov=apps --cov-report=term-missing
результат: exit 0, 1820 passed, 1 skipped in 916.69s, coverage 79.52%
```

Первый подготовительный запуск миграций остановился на `tenants.0012`, потому
что в изолированном локальном окружении не был задан обязательный
`FIELD_ENCRYPTION_KEY`. Это не было падением кода: тестовый volume был удалён,
для локального прогона задан фиксированный несекретный Fernet key, после чего
весь migration gate повторён с нуля на новой PostgreSQL и прошёл. После
проверок контейнеры, network и volume изолированного P0 compose-проекта удалены.

Документационные проверки P0 также прошли:

```bash
git diff --cached --check
# exit 0

perl -MFile::Basename=dirname -MFile::Spec -0777 -ne \
  'while (/\[[^\]]*\]\(([^)]+)\)/g) { $target=$1; $target =~ s/\s+"[^"]*"$//; next if $target =~ m{^(?:https?://|mailto:|#)}; $target =~ s/#.*$//; $target =~ s/^<|>$//g; next if $target eq q{}; $path=File::Spec->catfile(dirname($ARGV), $target); if (!-e $path) { print "$ARGV -> $target\n"; $bad=1 } } END { exit($bad ? 1 : 0) }' \
  AGENTS.md README.md docs/AVITO_FEED_CHANGESET_MANIFEST.md \
  docs/AVITO_FEED_ROADMAP.md docs/AVITO_FEED_SNAPSHOT_INVENTORY.md \
  docs/AVITO_FEED_STATUS.md docs/DEPLOYMENT.md \
  docs/ENGINEERING_EXECUTION_RULES.md docs/PRODUCTION_SECURITY.md \
  docs/RELEASE_CHECKLIST.md
# exit 0; отсутствующих относительных целей нет

diff \
  <(git diff-tree --no-commit-id --name-status -r \
    8ae8e265656dcae00a96111ac6477bb1d05e7e8f | sort) \
  <(awk -F'|' '/^\| [AM] \|/ {status=$2; path=$3; gsub(/[[:space:]]/, "", status); gsub(/^[[:space:]]*`|`[[:space:]]*$/, "", path); print status "\t" path}' \
    docs/AVITO_FEED_SNAPSHOT_INVENTORY.md | sort)
# exit 0; все 169 snapshot paths совпадают по status и path
```

Проверка staged-paths подтвердила, что P0 содержит только десять объявленных
документов: runtime-кода, settings, миграций и тестов в пакете нет.

### Проверка P1 observability

P1 содержит 16 production/config/operations-файлов, восемь test-файлов и один
runbook. Новых миграций нет. Public feed endpoint, lifecycle/product writer
changes, future `feed_poll` budget, private storage, cleanup, GC, `0039` и
worker wiring будущих пакетов исключены по hunks.

Узкий gate:

```text
docker compose run --rm django pytest \
  apps/core/tests/test_celery_observability.py \
  apps/core/tests/test_observability_periodic.py \
  apps/core/tests/test_queue_observability.py \
  apps/core/tests/test_sentry_scrubbing.py \
  apps/marketplaces/tests/test_avito.py::test_rate_limiter_creates_ttl_window_and_rejects_only_over_limit \
  apps/marketplaces/tests/test_avito.py::test_avito_request_emits_bounded_5xx_telemetry \
  apps/marketplaces/tests/test_avito.py::test_avito_request_emits_remote_429_and_network_error_telemetry \
  apps/products/tests/test_subscription_access_tasks.py::test_datasource_import_metrics_distinguish_retry_from_exhausted_failure \
  tests/test_production_host_contract.py tests/test_runtime_contract.py
результат: exit 0, 86 passed in 20.84s
```

Migration и static gates:

```text
docker compose run --rm django python manage.py makemigrations --check --dry-run
результат: exit 0, No changes detected

docker compose run --rm django python manage.py migrate --noinput
результат: exit 0, все существующие миграции применены на чистой PostgreSQL

docker compose run --rm django python manage.py migrate --noinput
результат: exit 0, No migrations to apply поверх схемы предыдущего пакета

docker compose run --rm django flake8 .
результат: exit 0
```

Полный backend gate:

```text
docker compose run --rm django pytest --cov=apps --cov-report=term-missing
результат: exit 0, 1859 passed, 1 skipped in 899.68s, coverage 79.63%
```

`git diff --check`, проверка Markdown-ссылок, отсутствие migrations/frozen
paths и неизменность `MARKETPLACE_FEED_*` hunks проходят перед P1 commit.
Production значения остаются
`legacy/legacy/disabled/false/legacy_public`. P1 foundation выложен с этими
значениями. Sentry dashboard, восемь metric monitors и alert
`P1 Production Critical Alerts` настроены; email action направлен команде
`#map-dodugir`.

Cron dead-man follow-up создал code-owned monitor
`map-celery-observability-collector` (`1674179`). После production release
monitor включён и получает регулярные `Okay` check-in. Безопасный
error-to-success test-fire повторно открыл issue `141940026`, выполнил правила
alert `773554` и закрыл issue после успешного check-in.

Follow-up gates: 21 observability/Sentry test и 89 production runtime/host
contract tests прошли; mypy, flake8, compileall, clean PostgreSQL migrations и
migration drift прошли. Повтор самого нового GitHub run после UTC midnight дал
зелёный full suite: 1863 passed. PR `#226` merged, push-main CI и release gate
зелёные. Production checkout имеет exact SHA `c2bc2eb`, все десять контейнеров
healthy, readiness отвечает HTTP 200, backup timers активны. P1 не добавлял
runtime-переключатели feed-системы; production оставался legacy-only.

### Проверка P2a: только расширение схемы

P2a физически выделен от текущего `main`. Он содержит только девять nullable
полей в `MarketplaceAccount`/`Listing`, миграцию `0020`, один concurrent partial
index в миграции `0021` и schema-contract tests. В пакете нет lifecycle-сервиса,
backfill, scheduler, task/view/admin wiring, новой настройки режима, private
storage, cleanup, GC или `0022+`.

Локальный PostgreSQL gate:

```text
python manage.py migrate --noinput
результат: exit 0, чистая PostgreSQL применена до marketplaces.0021

python manage.py makemigrations --check --dry-run
результат: exit 0, No changes detected

python manage.py migrate marketplaces 0019 --noinput
python manage.py migrate marketplaces 0021 --noinput
результат: exit 0, 0020/0021 успешно отменены и повторно применены

pytest apps/marketplaces/tests/test_status_lifecycle_expand.py -q
результат: exit 0, 6 passed

pytest apps/marketplaces/tests -q
результат: exit 0, 230 passed

flake8 .
mypy
mypy --check-untyped-defs --exclude '(^|/)(tests?|migrations)/' apps config backup
результат: exit 0, 619 и 326 source files без ошибок типов

pytest --cov=apps --cov-config=.coveragerc --cov-report=term-missing
результат: exit 0, 1868 passed, 1 skipped in 997.17s, coverage 79.65%

python manage.py spectacular --file /tmp/p2a-openapi-schema.yml \
  --validate --fail-on-warn
результат: exit 0
```

MigrationExecutor test создаёт существующие account/product/listing на текущей
схеме, переводит Marketplace с `0021` на `0019` и обратно на `0021`, затем
проверяет сохранность старых значений и `NULL` во всех новых колонках. Индекс
дополнительно проверяется через PostgreSQL catalog как valid/ready, без default
и `atthasmissing` у новых колонок.

PR `#227` merged в `decd480036353f08f26e5279d1056a71a4802172`. PR CI run
`32443968692` и push-main CI run `32445091312` завершены успешно. One-off
production release создал зашифрованный backup, применил ровно `0020` и `0021`
и завершил exact topology/readiness gate.

Post-deploy подтверждено:

- production checkout exact `decd480`, Git clean;
- все десять контейнеров healthy, restart count `0`, readiness HTTP 200;
- `0020` и `0021` применены;
- пять listing-колонок и четыре account-колонки nullable без DB defaults;
- `mkt_lst_acct_stat_due` valid/ready;
- backup timers active;
- manual production monitor run `32447097556` для exact SHA зелёный;
- за первые десять минут нет critical/traceback/internal-server-error matches
  в Django, Celery, frontend и Nginx.

На host остаётся не связанный с приложением failed unit
`cloud-init-hotplugd.service`: он упал 2026-08-20 19:04 UTC до P2a из-за
metadata hotplug detection. Application topology, readiness, backup freshness
и capacity от этого не пострадали; исправление ОС не входит в feed-пакет.

### Проверка P2b1 перед PR

P2b1 физически выделен от production `main`: 14 файлов, из них семь
production-файлов и около 871 новой production-строки. В пакете нет scheduler,
marketplace tasks/services/views/admin, runtime fencing, private storage,
cleanup или GC. Настройка production остаётся `legacy`.

Локальный PostgreSQL и application gate:

```text
python manage.py makemigrations --check --dry-run
результат: exit 0, No changes detected

python manage.py migrate marketplaces 0022 --noinput
python manage.py migrate marketplaces 0021 --noinput
python manage.py migrate marketplaces 0022 --noinput
результат: exit 0; 0022 применена, отменена и повторно применена
PostgreSQL catalog: mkt_acct_provider_due valid=true, ready=true

pytest -q apps/marketplaces/tests/test_listing_lifecycle.py \
  apps/marketplaces/tests/test_backfill_listing_status_lifecycle.py \
  tests/test_production_storage_settings.py
результат: exit 0, 123 passed

pytest -q apps/marketplaces/tests/test_status_lifecycle_expand.py
результат: exit 0, 7 passed

pytest -q apps/marketplaces/tests
результат: exit 0, 277 passed

flake8 .
mypy
mypy --check-untyped-defs --exclude '(^|/)(tests?|migrations)/' apps config backup
результат: exit 0; 624 и 328 source files без ошибок типов

python manage.py spectacular --file /tmp/openapi-p2b1.yml \
  --validate --fail-on-warn
результат: exit 0

pytest --cov=apps --cov-config=.coveragerc --cov-report=term-missing
результат: exit 0, 1922 passed, 1 skipped in 954.83s, coverage 79.85%
```

Ручной command preflight в `legacy` разрешил только bounded `--dry-run`
(`updated=0`) и ожидаемо отказал apply с `CommandError`. Команда не делает
provider-вызовов и нигде не зарегистрирована в periodic scheduler.

### Production release P2b1

PR `#229` merged в `de0d202d084af51169ce284cdaf117b0335cb7e5`. PR CI run
`32467660130` и push-main CI run `32469548088` завершены успешно для точных
head/merge SHA. Автоматический Deploy run `32471517904` ожидаемо получил
`skipped`, потому что repository variable `PROD_DEPLOY_ENABLED=false`.

Документированный one-off release:

- до release production был на `decd480`, Git clean;
- в защищённый `.env` добавлена только строка
  `AVITO_STATUS_LIFECYCLE_MODE=legacy`; права остались `600 root:root`;
- encrypted backup загружен как
  `postgres/daily/2026/08/20260821T102448Z_de0d202d084a_b53848c10edf.dump.age`;
- применена ровно `marketplaces.0022_account_status_lifecycle_concurrent_index`;
- production checkout exact `de0d202`, Git clean;
- `mkt_acct_provider_due` в PostgreSQL catalog имеет `valid=true` и
  `ready=true`;
- runtime setting равен `legacy`; scheduled backfill/lifecycle tasks: `0`;
- bounded production dry-run нашёл 10 active candidates в одном аккаунте,
  сообщил `would_update=10`, но `updated=0`; apply не запускался;
- все десять контейнеров healthy, initial restart count `0`, readiness HTTP
  200, оба backup timer active;
- manual production monitor run `32472777118` для exact SHA зелёный;
- через десять минут exact SHA и clean Git сохранились, все десять контейнеров
  оставались healthy, readiness HTTP 200, critical/traceback/unhandled/internal
  server error matches в application-логах: `0`.

Во время graceful drain старый `celery_beat` проигнорировал повторные SIGTERM и
оставался sleeping внутри общего `docker compose stop -t 3700`. Ingress и
workers уже были остановлены, beat не выполняет сами business tasks, а его
логи были пустыми. Чтобы не держать production без ingress до часового timeout,
оператор завершил только старый beat через SIGKILL; тот же release затем создал
backup, применил миграцию и поднял новый healthy beat с restart count `0`.
Разделение короткого beat-stop timeout и длинного worker drain, а также
проверка signal propagation записаны как отдельный release-tooling backlog и
не исправляются внутри P2b1.

Для смешанного WIP snapshot всё ещё не выполнены как единый актуальный gate:

- применение всех миграций на чистой PostgreSQL;
- полный backend `pytest` после последних изменений;
- проверка обновления существующей базы;
- проверка неизменности старой отправки;
- тест одной генерации на 10 000 объявлений;
- реальная проверка Avito и HTTP 307;
- реальная проверка приватного versioned bucket, IAM и восстановления после
  сбоев.

Snapshot и оставшиеся P2b2–P7 не являются кандидатами на единый релиз. Успешные
P0/P1/P2a/P2b1 gates подтверждают только выделенные пакеты и не верифицируют
оставшийся код из snapshot.

## Текущий bounded cleanup-срез

`feed_run_dispatch_terminal_cleanup.py` и отдельная management-команда доведены
до `CODE_READY`. Они удаляют только строго определённый fixed `CANCELLED`
маркер после explicit dry-run, quiescence и отдельного подтверждения purge;
любой fence, business reference, bindable/colliding identity или schema drift
даёт fail-closed отказ. Cleanup всегда требует повторить полный backfill от
начала и сам не доказывает convergence.

Cleanup bounded по fixed UTC cutoff, `(created_at, dispatch UUID)` keyset,
batch не больше 100 и общему лимиту не больше 10 000 строк. Каждая строка
удаляется в собственной транзакции. Ошибка на поздней строке может оставить
ранее подтверждённые удаления закоммиченными и намеренно не раскрывает
persisted payload/ID в сообщении. Recovery — после устранения причины повторить
cleanup с начала с тем же cutoff (операция идемпотентна), затем повторить полный
backfill с начала. Нельзя угадывать cursor по тексту ошибки.

Этот срез не является `VERIFIED`, потому что:

1. такие записи возникают только после будущего backfill;
2. production backfill ещё не выполнялся;
3. PostgreSQL regressions и полный backend suite после последней правки не
   запускались;
4. production dry-run должен сначала доказать, что удалять вообще что-то
   нужно;
5. более ранние проверки проекта всё ещё не закрыты.

Срез остаётся частью будущего P7 и не входит в разрешённый к выкладке пакет.
Cleanup/backfill и auto-applied `0039` запрещено выпускать одним release:
`0039` возможен только в отдельном следующем rollout после фактической очистки,
полного повторного backfill и независимого fleet/broker drain evidence.

## Активный шаг

P0–P3 завершены. P3 работает в production на exact commit `f1881f1`, но его
новый durable runner выключен: владельцем отправки остаётся legacy-код. P4
выделен в ветку `codex/p4-stable-feed-endpoint`; следующий release добавляет
схему и код stable endpoint в выключенном состоянии. Реальный Avito 307 canary
и изменение Autoload-профиля требуют отдельного решения после deploy.

### Локальная проверка P4 перед PR

P4 содержит одну миграцию `0025`, stable capability endpoint,
account-scoped inspect/prepare/migrate/reconcile workflow и защиту смены
credentials/состояния аккаунта. В package нет миграций `0026+`, feed intents,
private artifact serving/storage, cleanup, GC, `0039` или worker activation.

Фактические gates финального runtime-среза:

```text
pytest -q \
  apps/marketplaces/tests/test_migrate_marketplace_feed_profile_command.py \
  apps/marketplaces/tests/test_feed_endpoint_schema.py \
  apps/marketplaces/tests/test_feed_endpoint_route.py \
  apps/marketplaces/tests/test_feed_profile_migration.py
результат: 105 passed in 23.80s

pytest -q
результат: 2198 passed, 1 skipped in 859.65s

python manage.py makemigrations --check --dry-run
результат: No changes detected

clean PostgreSQL: migrate --noinput
результат: marketplaces.0025 и все остальные миграции применены успешно

upgrade/rollback PostgreSQL:
marketplaces.0024 -> 0025 -> 0024 -> 0025
результат: все четыре перехода успешны

flake8 по всем изменённым Python-файлам
mypy: 646 source files
mypy --check-untyped-defs: 335 source files
git diff --check
frontend ESLint и TypeScript typecheck
результат: exit 0
```

Настройки release остаются
`legacy/legacy/disabled/false/legacy_public`; signing keys отсутствуют. P4
ещё не считается production-deployed до зелёных PR/main CI, schema deploy и
post-deploy health/setting checks.

### Локальная проверка P2b2 перед PR

P2b2 физически выделен от production `main`: два production-файла,
1 368 новых production-строк, один новый test-файл и три обновлённых документа.
Новых миграций, scheduler activation, private storage, cleanup, GC, feed-run и
stable endpoint частей в пакете нет. Личные настройки
`.claude/settings.local.json` отсутствуют. Production-режим остаётся `legacy`.

Фактические проверки точного финального diff:

```text
python3 -m compileall -q apps/marketplaces/services.py \
  apps/marketplaces/tasks.py apps/marketplaces/tests/test_status_fencing.py
git diff --check
результат: exit 0

pytest -q apps/marketplaces/tests/test_status_fencing.py
результат: exit 0, 15 passed in 34.99s

pytest -q apps/marketplaces/tests
результат: exit 0, 292 passed in 117.27s

pytest --cov=apps --cov-config=.coveragerc --cov-report=term-missing
результат: exit 0, 1937 passed, 1 skipped in 1078.35s, coverage 79.70%

flake8 .
mypy
mypy --check-untyped-defs --exclude '(^|/)(tests?|migrations)/' \
  apps config backup
результат: exit 0; 625 и 328 source files без ошибок типов

python manage.py makemigrations --check --dry-run
результат: exit 0, No changes detected

python manage.py spectacular --file /tmp/openapi-p2b2.yml \
  --validate --fail-on-warn
результат: exit 0
```

Дополнительно все существующие миграции были успешно применены на чистой
PostgreSQL до `marketplaces.0022`; последующая финальная правка затронула только
runtime services/tasks и тесты, а повторный migration-drift остался чистым.
Legacy-тест подтверждает, что lifecycle-поля остаются `NULL`, когда режим
`AVITO_STATUS_LIFECYCLE_MODE=legacy`.

### Production release P2b2

PR `#232` merged в `0ef04de90ced8817ffe6edf2e775a4efda3d1784`. PR CI run
`32491344632` и push-main CI run `32493682105` завершены успешно для точных
head/merge SHA. Автоматический Deploy run `32495874817` ожидаемо получил
`skipped`, потому что repository variable `PROD_DEPLOY_ENABLED=false`.

Документированный one-off release:

- до release production был на `de0d202`, Git clean;
- production setting сохранился `AVITO_STATUS_LIFECYCLE_MODE=legacy`, а пять
  будущих feed-переключателей отсутствуют — runtime legacy-only по конструкции;
- создан encrypted backup
  `postgres/daily/2026/08/20260821T151024Z_0ef04de90ced_fff802b3a557.dump.age`;
- новых миграций не было; применённый head остаётся `marketplaces.0022`;
- индексы `mkt_lst_acct_stat_due` и `mkt_acct_provider_due` valid/ready;
- production checkout exact `0ef04de`, Git clean;
- lifecycle periodic tasks, remote observations, listing/account claims: `0`;
- старые Nginx, Django, frontend, оба worker и Beat остановились graceful без
  SIGKILL;
- все десять контейнеров healthy, restart count `0`, readiness HTTP 200, оба
  backup timer active;
- manual production monitor run `32496429816` для exact SHA зелёный;
- через десять минут exact SHA и clean Git сохранились, все контейнеры healthy,
  а critical/traceback/unhandled/internal-server-error/5xx matches в Django,
  Celery, frontend и Nginx равны `0`.

### Проверка active listings и P2c tenant visibility

Пользователь сообщил: в Avito нет активных объявлений, но дашборд показывает
10. Production-проверка подтвердила ровно 10 локальных
`Listing.status=active`, все у одного включённого marketplace account и все с
`external_id`. Дашборд напрямую считает это локальное поле.

Bounded read-only GET canary затем получил точный текущий provider truth:

- все 10 item GET вернули `status=active`;
- account-wide `GET /core/v1/items?status=active` вернул 14 active items;
- все 10 локальных external ID присутствуют в этом active list;
- OAuth credentials соответствуют configured Avito account;
- проверенный item дополнительно вернул `start_time` и `finish_time`, причём
  фактический срок действует до 2026-09-12.

Следовательно, замена 10 на 0 или принудительное архивирование были бы
неверными. P2c вместо этого делает источник числа понятным и предупреждает о
сроке, который подтверждает сам Avito:

- dashboard подписывает число как `Активные в MAP`;
- listing list и drawer показывают `last_sync_at` как время проверки Avito;
- active response с валидным `finish_time` создаёт tenant notice за
  14/7/3/1/0 дней;
- одно logical notice имеет стабильный неперсональный event key, coalescing в
  coordination cache и durable per-channel deduplication;
- устаревший CAS response, не-active status, отсутствующий/невалидный
  `finish_time` и срок больше 14 дней не создают notice;
- canonical status, lifecycle mode, scheduler и feed-механизм не меняются.

P2c затрагивает пять production-файлов в двух подсистемах, два test-файла и эти
три документа; новых миграций и настроек нет. Локальные gates:

```text
pytest -q apps/marketplaces/tests/test_status_fencing.py
результат: 25 passed in 24.10s

pytest -q apps/marketplaces/tests
результат: 303 passed in 95.91s

pytest --cov=apps --cov-config=.coveragerc --cov-report=term-missing
результат: 1948 passed, 1 skipped in 932.90s, coverage 79.72%

python manage.py makemigrations --check --dry-run
результат: No changes detected

flake8 .
mypy
mypy --check-untyped-defs --exclude '(^|/)(tests?|migrations)/' \
  apps config backup
результат: exit 0; 625 и 328 source files без ошибок типов

python manage.py spectacular --file /tmp/avito-expiry-openapi.yml \
  --validate --fail-on-warn
результат: exit 0

frontend: typecheck, ESLint, 25 unit tests
frontend production build: Next.js 16.3.0 webpack, 21 pages
результат: exit 0
```

Первый одноразовый pytest container остановился до тестов из-за невалидного
локального `FIELD_ENCRYPTION_KEY`; тот же gate повторён с фиксированным
несекретным Fernet test key и прошёл. Turbopack build не принял внешний
`node_modules` symlink временного checkout; тот же production build выполнен
поддерживаемым Next.js webpack mode и прошёл.

### Production release P2c

PR `#234` merged в `1f053674617c491abeeac60a8afe7376943aa5bb`. PR CI run
`32507225396` и push-main CI run `32509442153` завершены успешно для
точных head/merge SHA. Автоматический Deploy run `32511642072` ожидаемо
получил `skipped`, потому что repository variable
`PROD_DEPLOY_ENABLED=false`.

Документированный one-off release:

- до release production был на `0ef04de`, Git clean, все десять
  контейнеров healthy, readiness HTTP 200 и оба backup timer active;
- создан encrypted backup
  `postgres/daily/2026/08/20260821T181008Z_1f053674617c_135d50f0af73.dump.age`;
- новых миграций нет; применённый head остался
  `marketplaces.0022_account_status_lifecycle_concurrent_index`;
- production checkout exact `1f053674`, Git clean;
- `AVITO_STATUS_LIFECYCLE_MODE=legacy`, а пять будущих feed-переключателей
  отсутствуют и в environment, и в Django settings;
- все десять контейнеров healthy, restart count `0`, readiness HTTP 200,
  exact topology и оба backup timer зелёные;
- serializer отдаёт `last_sync_at`; все 10 active listings имеют время
  provider-проверки;
- первый плановый цикл на новом коде в `18:15 UTC` повторно обновил
  все 10 `last_sync_at` и сохранил provider-confirmed `active`;
- два разных listing event key вошли в порог 14 дней; создано по
  одной Telegram delivery на event, обе получили `sent`, дублей нет;
- manual production monitor run `32512208535` для exact SHA зелёный;
- через десять минут exact SHA и clean Git сохранились, все десять
  контейнеров остались healthy с restart count `0`, readiness отвечал
  HTTP 200, два notification event не дублировались, а
  critical/traceback/unhandled/internal-server-error matches в application logs и
  Nginx 5xx равны `0`.

## Состояние разделения

| Шаг | Статус |
|---|---|
| Отдельный `not-for-merge` WIP snapshot | `VERIFIED` 2026-08-20, commit `8ae8e26` |
| P0 status, rules, roadmap и точная карта 169 snapshot paths | `VERIFIED` 2026-08-20 |
| P0 Markdown links, inventory parity и `git diff --check` | `VERIFIED` 2026-08-20 |
| P0 clean PostgreSQL migrations и migration drift | `VERIFIED` 2026-08-20 |
| P0 полный backend suite, coverage и flake8 | `VERIFIED` 2026-08-20 |
| Физическое выделение P1 observability по hunks | `VERIFIED` 2026-08-20 |
| P1 narrow observability/Sentry/runtime/host tests | `VERIFIED` 2026-08-20, 86 passed |
| P1 migrations, full backend, coverage и flake8 | `VERIFIED` 2026-08-20, 1859 passed, 1 skipped |
| P1 foundation production release | `DEPLOYED_OFF` 2026-08-20, commit `8710007` |
| P1 Sentry Cron dead-man release | `VERIFIED` 2026-08-21, PR `#226`, production `c2bc2eb` |
| P2a schema `0020`–`0021` | `DEPLOYED_LEGACY_ONLY` 2026-08-21, PR `#227`, production `decd480` |
| P2a post-deploy monitor/schema/log observation | `VERIFIED` 2026-08-21 |
| P2b1 lifecycle/index/backfill `0022` | `DEPLOYED_LEGACY_ONLY`, PR `#229`, production `de0d202` |
| P2b1 monitor/schema/log observation | `VERIFIED` 2026-08-21 |
| P2b2 runtime fencing/dual-write | `DEPLOYED_LEGACY_ONLY`, PR `#232`, production `0ef04de` |
| P2b2 monitor/health/log observation | `VERIFIED` 2026-08-21 |
| Avito 0 / dashboard 10: bounded provider canary | `VERIFIED`; Avito API подтверждает все 10 active |
| P2c tenant status clarity/expiry notices | `DEPLOYED_LEGACY_ONLY`, PR `#234`, production `1f05367` |
| P3 durable feed foundation `0023`–`0024` | `DEPLOYED_OFF`, PR `#244`, production `f1881f1` |
| P4 stable endpoint/profile `0025` | `LOCAL_VERIFIED`, PR/deploy pending |
| P5 feed intents/recovery | `NOT_STARTED` |
| Решение, нужен ли P6 сейчас | `NOT_STARTED` |
| P7 cleanup/GC/0039 | `FROZEN` |

Физическое разделение не выполняется автоматически: общие файлы
`models.py`, `services.py`, `tasks.py` и settings нужно делить по
отдельным diff-hunks, после чего сразу проверять каждый получившийся пакет.
Точная карта находится в `AVITO_FEED_CHANGESET_MANIFEST.md`.
