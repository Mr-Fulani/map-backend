# Текущее состояние работ по фидам Avito

Обновлено: 2026-08-20.

Исходный WIP сохранён как локальный `not-for-merge` snapshot:

- branch `codex/wip-not-for-merge-avito-scaling-20260820`;
- commit `8ae8e265656dcae00a96111ac6477bb1d05e7e8f`;
- base `7415ccca0ae54fccc9cb389704fa8e183feea213`;
- 65 изменённых и 104 добавленных файла, всего 169;
- diff snapshot: 61 114 добавлений и 938 удалений.

`.claude/settings.local.json` намеренно исключён и не входит ни в один пакет.
Snapshot хранит смешанный незавершённый WIP, не предназначен для merge или
release и не означает, что его код присутствует в P0-ветке.

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

Для смешанного WIP snapshot всё ещё не выполнены как единый актуальный gate:

- применение всех миграций на чистой PostgreSQL;
- полный backend `pytest` после последних изменений;
- проверка обновления существующей базы;
- проверка неизменности старой отправки;
- тест одной генерации на 10 000 объявлений;
- реальная проверка Avito и HTTP 307;
- реальная проверка приватного versioned bucket, IAM и восстановления после
  сбоев.

Snapshot и будущие P1–P7 не являются кандидатами на релиз. Успешный P0
baseline подтверждает чистый исходный `main`, но не верифицирует код из
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

Завершён только пакет `P0` из
[`AVITO_FEED_ROADMAP.md`](AVITO_FEED_ROADMAP.md): зафиксировать состав
изменений, разделить их, получить чистую базовую проверку и не менять рабочие
production-флаги. P1 observability требует отдельной активации после завершения
и отчёта P0; он пока не активирован.

## Состояние разделения

| Шаг | Статус |
|---|---|
| Отдельный `not-for-merge` WIP snapshot | `VERIFIED` 2026-08-20, commit `8ae8e26` |
| P0 status, rules, roadmap и точная карта 169 snapshot paths | `VERIFIED` 2026-08-20 |
| P0 Markdown links, inventory parity и `git diff --check` | `VERIFIED` 2026-08-20 |
| P0 clean PostgreSQL migrations и migration drift | `VERIFIED` 2026-08-20 |
| P0 полный backend suite, coverage и flake8 | `VERIFIED` 2026-08-20 |
| Физическое разделение diff на commits/PR P1–P7 | `NOT_STARTED` |
| Проверки пакета P1 | `NOT_STARTED` |
| Проверки и deploy пакетов P2–P5 | `NOT_STARTED` |
| Решение, нужен ли P6 сейчас | `NOT_STARTED` |
| P7 cleanup/GC/0039 | `FROZEN` |

Физическое разделение не выполняется автоматически: общие файлы
`models.py`, `services.py`, `tasks.py` и settings нужно делить по
отдельным diff-hunks, после чего сразу проверять каждый получившийся пакет.
Точная карта находится в `AVITO_FEED_CHANGESET_MANIFEST.md`.
