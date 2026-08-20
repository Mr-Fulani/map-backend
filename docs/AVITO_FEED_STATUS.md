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
выложен в production commit `8710007c37eba1de000475fc8024fe850f97ef1b`.
Текущий P1 follow-up PR `#226` добавляет только Sentry Cron dead-man collector,
его тесты и runbook; feed-код и feed-флаги он не меняет.

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
значения:

```text
MARKETPLACE_FEED_RUN_MODE=legacy
MARKETPLACE_FEED_INGRESS_MODE=legacy
MARKETPLACE_FEED_ARTIFACT_MODE=disabled
MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=false
MARKETPLACE_FEED_STORAGE_MODE=legacy_public
```

Новая система настройками выключена. Она не должна включаться в рамках
текущего набора изменений.

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
`map-celery-observability-collector` (`1674179`). Реальный pre-deploy test-fire
создал missed-check-in issue `141940026`, вызвал два alert actions и закрыл issue
после успешного check-in. Monitor временно disabled до release follow-up, чтобы
не создавать ложные пропуски без ещё не выложенного producer-кода.

Follow-up gates: 21 observability/Sentry test и 89 production runtime/host
contract tests прошли; mypy, flake8, compileall, clean PostgreSQL migrations и
migration drift прошли. GitHub full suite дал 1862 passed и один известный
baseline failure в retention-тесте. Тот же `assert 0 == 1` независимо
воспроизведён на чистом base commit `8710007`: тест смешивает
`timezone.localdate()` и UTC `timezone.now().date()` в интервале 00:00–03:00
Europe/Istanbul. Retention не меняется внутри P1; CI повторяется после UTC
midnight.

Для смешанного WIP snapshot всё ещё не выполнены как единый актуальный gate:

- применение всех миграций на чистой PostgreSQL;
- полный backend `pytest` после последних изменений;
- проверка обновления существующей базы;
- проверка неизменности старой отправки;
- тест одной генерации на 10 000 объявлений;
- реальная проверка Avito и HTTP 307;
- реальная проверка приватного versioned bucket, IAM и восстановления после
  сбоев.

Snapshot и будущие P2–P7 не являются кандидатами на релиз. Успешные P0/P1
gates подтверждают выделенные пакеты, но не верифицируют оставшийся код из
snapshot.

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

P0 завершён, P1 foundation выложен. Активный пакет — завершение P1 Cron
dead-man release и период наблюдения без включения новых feed-механизмов.
Владелец продукта 2026-08-21 явно разрешил продолжить roadmap; поэтому P2
считается следующим разрешённым пакетом, но его код не смешивается с P1 и
начинается только после закрытия текущего release gate.

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
| P1 Sentry Cron dead-man release и период наблюдения | `IN_PROGRESS`, PR `#226` |
| P2 lifecycle activation | `AUTHORIZED` 2026-08-21, ждёт закрытия P1 gate |
| Физическое разделение diff на commits/PR P2–P7 | `NOT_STARTED` |
| Проверки и deploy пакетов P2–P5 | `NOT_STARTED` |
| Решение, нужен ли P6 сейчас | `NOT_STARTED` |
| P7 cleanup/GC/0039 | `FROZEN` |

Физическое разделение не выполняется автоматически: общие файлы
`models.py`, `services.py`, `tasks.py` и settings нужно делить по
отдельным diff-hunks, после чего сразу проверять каждый получившийся пакет.
Точная карта находится в `AVITO_FEED_CHANGESET_MANIFEST.md`.
