# Текущее состояние работ по фидам Avito

Обновлено: 2026-08-21.

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
feed-флаги P1 не менял. Активный пакет P2 разделён на P2a и P2b, чтобы каждый
release соблюдал ограничение не более двух миграций.

Этот файл — единственный источник правды о текущей стадии работ. Roadmap
находится в [`AVITO_FEED_ROADMAP.md`](AVITO_FEED_ROADMAP.md), а обязательные
правила выполнения — в
[`ENGINEERING_EXECUTION_RULES.md`](ENGINEERING_EXECUTION_RULES.md).
Точная карта разделения файлов и тестов:
[`AVITO_FEED_CHANGESET_MANIFEST.md`](AVITO_FEED_CHANGESET_MANIFEST.md).

## Решение: новые механизмы заморожены

До отдельного решения владельца продукта запрещено:

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

В текущем production commit эти имена ещё не определены в Django/Compose и
отсутствуют в `.env`: альтернативного runtime-пути нет, поэтому система
legacy-only по конструкции. P2a не добавляет и не читает эти переключатели.

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

Для смешанного WIP snapshot всё ещё не выполнены как единый актуальный gate:

- применение всех миграций на чистой PostgreSQL;
- полный backend `pytest` после последних изменений;
- проверка обновления существующей базы;
- проверка неизменности старой отправки;
- тест одной генерации на 10 000 объявлений;
- реальная проверка Avito и HTTP 307;
- реальная проверка приватного versioned bucket, IAM и восстановления после
  сбоев.

Snapshot и оставшиеся P2b–P7 не являются кандидатами на единый релиз. Успешные
P0/P1/P2a gates подтверждают только выделенные пакеты и не верифицируют
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

## Активный разрешённый шаг

P0, P1 и P2a завершены и работают в production. Активный следующий пакет —
P2b: migration `0022`, lifecycle/backfill/fencing с режимом `legacy` и
выключенным scheduler. Он выделяется и проверяется отдельно от P2a. P3 не
начинается до отдельного закрытия всего P2.

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
| P2b lifecycle/backfill/fencing `0022` | `AUTHORIZED`, следующий отдельный пакет |
| Физическое разделение diff на commits/PR P3–P7 | `NOT_STARTED` |
| Проверки и deploy пакетов P3–P5 | `NOT_STARTED` |
| Решение, нужен ли P6 сейчас | `NOT_STARTED` |
| P7 cleanup/GC/0039 | `FROZEN` |

Физическое разделение не выполняется автоматически: общие файлы
`models.py`, `services.py`, `tasks.py` и settings нужно делить по
отдельным diff-hunks, после чего сразу проверять каждый получившийся пакет.
Точная карта находится в `AVITO_FEED_CHANGESET_MANIFEST.md`.
