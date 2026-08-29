# Текущее состояние работ по фидам Avito

Обновлено: 2026-08-29.

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
`f1881f123a0bbd4fc2534ba746eaff19af8f851b`. P4 merged через PR `#245` и
выложен выключенным на production commit `9061ebb`. Безопасный P5 foundation
merged через PR `#246` и выложен в production commit
`2e9958cd6a85aef0e712b6bed95d94836bdb8db7`; production остался в legacy.
P5 writer/legacy-delivery activation merged через PR `#247`, выложен exact
commit `9c23a6b37264fb26be9af78876af034b8d1cb508` и прошёл legacy-only production
gate. Settings-gate для парного P5 `dual_write` observation merged через PR
`#248` в `54b87f286b1e6a318fda6acf1abfa266fdd48bd2`, но само production-переключение
выполнено отдельно. P6 private artifacts merged через PR `#249`, bounded
follow-up/recovery — через PR `#250`–`#254`, account-scoped cutover — через PR
`#255`. PR `#256` завершил fleet-default rollout. PR `#257`–`#270` закрыли
ускорение CI, защиту удаления marketplace account и обнаруженные при реальной
эксплуатации ошибки публикации/статусов. PR `#271` добавил multi-account и
cross-tenant acceptance, PR `#272` уточнил общий и account-scoped лимиты в
Billing, PR `#261` ограничил остановку Beat отдельным timeout. Текущий
production работает на exact commit
`1c0030532f7fa4bd5357d48b39a5e938261931b5`.

Этот файл — единственный источник правды о текущей стадии работ. Roadmap
находится в [`AVITO_FEED_ROADMAP.md`](AVITO_FEED_ROADMAP.md), а обязательные
правила выполнения — в
[`ENGINEERING_EXECUTION_RULES.md`](ENGINEERING_EXECUTION_RULES.md).
Точная карта разделения файлов и тестов:
[`AVITO_FEED_CHANGESET_MANIFEST.md`](AVITO_FEED_CHANGESET_MANIFEST.md).

## Решение: P6 fleet-default включён и укреплён, P7 отложен

P0–P6 внедрены. Новая цепочка Avito является штатной production-цепочкой, а не
экспериментом одного аккаунта:

- изменения каталога записываются как durable intent;
- worker создаёт версионированный XML в закрытом Yandex Object Storage;
- MAP выдаёт Avito постоянный stable URL, который перенаправляет на точную
  короткоживущую ссылку конкретной версии;
- новый успешно подключённый Avito-аккаунт получает managed endpoint и
  onboarding автоматически, без ручного allowlist;
- пока Avito не подтвердил endpoint, публикация ждёт безопасной сверки и не
  откатывается к публичной legacy-загрузке;
- неоднозначные внешние POST/PUT не повторяются вслепую.

Старый onboarding физически остаётся только аварийной совместимостью до
отдельного observation и удаления в будущем reviewed пакете.

P7 не является условием подключения следующего маркетплейса. Он относится к
retention, cleanup и удалению старых служебных данных. Пока объём хранения не
мешает эксплуатации и не утверждена политика удаления/восстановления, P7
остаётся `DEFERRED`. Актуальный остаточный долг ведётся в
[`../TECH_DEBT.md`](../TECH_DEBT.md), а следующий продуктовый этап — в
[`MARKETPLACE_EXPANSION_ROADMAP.md`](MARKETPLACE_EXPANSION_ROADMAP.md).

Read-only production snapshot 2026-08-29 подтвердил отсутствие срочности P7:
6 feed runs, 5 artifact-записей, 6 upload attempts, oldest record от
2026-08-26, размер всей базы PostgreSQL `105126935` bytes. Удаление данных при
таком объёме не даёт эксплуатационной выгоды, сопоставимой с риском.

До отдельного нового решения запрещено:

- добавлять миграции после точечной P6 runtime-коррекции `0031` или новые
  режимы;
- продолжать `0039`, retention delete, автоматическое удаление файлов или GC;
- удалять, отвязывать или перезаписывать artifact/evidence записи и S3-версии;
- выполнять массовую миграцию старых профилей отдельным sweep-процессом;
- физически удалять legacy-код до подтверждённого периода наблюдения;
- считать внешний Avito `processing` причиной для повторного POST.

В уже включённом P6 работает:

- private artifact schema, exact-version upload/readback и fail-closed serving;
- отдельные IAM credentials, folder-owner/KMS/versioning preflight и presigner;
- потоковый тест 10 000 объявлений и необходимые regression/test-fixes;
- один проверенный реальный account `4` и общий fleet-default admission;
- автоматическое резервирование stable endpoint при создании Avito-аккаунта;
- регистрация stable URL в Avito с tenant/account fencing;
- durable/private delivery для всех готовых аккаунтов без allowlist;
- удержание публикации без legacy fallback, пока endpoint не подтверждён.

## Почему введена заморозка

Snapshot меняет 169 файлов и добавляет больше 61 тысячи строк. Такой объём и
смешение P0–P7 нельзя считать одним проверяемым релизом.

Работа ушла дальше необходимой цели: до цельной проверки уже написанного были
начаты будущие механизмы приватных файлов, их удаления и дополнительной защиты
служебных заданий.

## Что сейчас работает в production

Production settings во всех Django/Celery consumers:

```text
MARKETPLACE_FEED_RUN_MODE=durable
MARKETPLACE_FEED_INGRESS_MODE=dual_write
AVITO_STATUS_LIFECYCLE_MODE=dual_write
MARKETPLACE_FEED_ARTIFACT_MODE=active
MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS=
MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=false
MARKETPLACE_FEED_STORAGE_MODE=stable_bridge
```

Пустой allowlist означает fleet-default, а не выключение. Account `4`
продолжает ту же private-цепочку, а новый успешно
подключённый Avito-аккаунт получает managed stable URL автоматически. Если
регистрация URL у Avito ещё не доказана, публикация ждёт и повторяет безопасную
проверку; старый public upload не вызывается.

`MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=false` запрещает массовый sweep
старых профилей, но не выключает штатный onboarding нового аккаунта.

### Реальный account 4

После fleet switch подтверждено:

- endpoint `private_generation`, `verified`, `serve_enabled=true`;
- текущий artifact существует, listing count `10`;
- intent/dispatched/source revisions равны `2`, endpoint artifact revision —
  `2`;
- stable endpoint отвечает `307` на exact version с коротким TTL;
- uncertain runs и unresolved PUT attempts: `0`.

Avito upload `587751397`, отправленный 2026-08-26 20:54 UTC, получил terminal
результат. Read-only production-проверка 2026-08-27 подтвердила durable run
`b2084883-4914-4f1a-9d35-e40562c22e73` в состоянии `succeeded`, revision `23`;
provider report полностью обработан 2026-08-27 00:31 UTC, `last_error` пуст,
следующий retry и lease отсутствуют. Предыдущий provider run остался
`587591356`, поэтому новая отправка привязана к отдельному точному ID.

Схема run намеренно сводит Avito `success` и `success_warning` в один безопасный
terminal `succeeded` и не сохраняет исходный вариант отдельно. Blocking report
для этого поколения пуст: rejected/pending/error counts равны `0`. Upload ledger
содержит ровно одну attempt `1`: artifact атомарно привязан по ответу одиночного
PUT, projection содержит `10` листингов, endpoint остаётся `verified`.
Дублирующего PUT/generation, unresolved attempt и uncertain run нет.

### Release evidence fleet-default

- PR `#256`, PR CI run `33018719809`, job `98343434543`: `success`;
- merge SHA: `0762ab578dda40aeff3178b6aa4e69247b40eae7`;
- merge tree и проверенный PR head tree совпали:
  `d52ec2b876d2a8e9da5eaf5df799c087bc932691`;
- дублирующий push-main CI `33020511166` отменён только после доказанного
  совпадения tree SHA, затем выполнен канонический manual deploy;
- новых миграций нет (`No migrations to apply`);
- production checkout exact и clean, 10 контейнеров healthy, restart count
  `0`, readiness HTTP `200`, оба backup timer active;
- application critical matches и Nginx 5xx matches: `0`.

Release backup:

```text
postgres/daily/2026/08/20260826T224643Z_0762ab578dda_b5a32cbaafdd.dump.age
bytes: 5514721
sha256: 30120e86893fb2dcff728b08468d4c91b690ec6f1ff16ce12dd9a2d24245bb5e
```

### Operational hardening после fleet-default

Последовательные PR `#257`–`#270` не активировали P7 и не добавляли GC или
удаление объектов:

- `#257` и `#259` разделили CI на параллельные shards и исправили повторное
  использование coverage artifacts;
- `#258` и `#260` запретили небезопасное одиночное и массовое удаление
  MarketplaceAccount с feed history/evidence;
- `#262` ограничил polling/statistics и сделал onboarding восстанавливаемым;
- `#263`–`#267` закрыли координацию retries, накопление первого feed window,
  восстановление безопасно повторяемой generation и последующие immutable
  private artifacts;
- `#268` синхронизировал фактический результат текущего Avito upload;
- `#269` добавил field-level preflight и корректные условные требования Avito;
- `#270` отделил исправленное текущее состояние карточки от исторической причины
  отклонения provider-а.

Финальный PR `#270` прошёл полный CI run `33251484132`, merged commit
`65bdf213c540de15f69f105a2ae3fc4813d59912` был выложен Deploy run
`33253919127`. После release production checkout совпал с exact SHA, все десять
сервисов были healthy, readiness отвечал HTTP `200`, deploy gate возвращён в
`false`. Для карточки `OEM0099FONR` production serializer подтвердил
`rejection_ready_to_retry=true`, отсутствие текущих preflight/OEM errors и
tenant-facing статус «Исправлено — отправьте снова».

### Что ещё не доказано и что дальше

- Второго реального Avito Autoload аккаунта пока нет. Account `4` доказывает
  private storage, exact-version serving, Avito trigger и durable polling на
  реальном provider. Отдельный acceptance несколькими фейковыми аккаунтами
  проверяет два аккаунта одного tenant и первый аккаунт нового tenant, включая
  уникальные endpoint/URL, tenant fencing и независимый Autoload profile POST.
  Реальное второе подключение остаётся observation, а не блокировкой fleet-кода.
- Terminal observation upload `587751397` закрыт успешным durable outcome;
  исходный вариант Avito `success`/`success_warning` отдельно не различается,
  blocking report пуст.
- Fleet runtime должен пройти согласованный период наблюдения до решения об
  удалении аварийного legacy-кода. P7 начинается только при отдельной
  необходимости retention/cleanup, а не автоматически после observation.
- Обычная продуктовая разработка, не затрагивающая P7/GC/delete/`0039`, не
  обязана ждать этого observation window.

## Историческое состояние исходного snapshot

Таблица ниже описывает только сохранённый WIP на 2026-08-20, а не текущий
production runtime. Актуальное состояние пакетов приведено ниже отдельно.

| Часть | Состояние | Можно включать? |
|---|---|---|
| Старая отправка на дату snapshot | Работала на 2026-08-20 | Исторический факт |
| Учёт изменений товаров и объявлений | Код написан, общая проверка не завершена | Нет |
| Надёжные задания на повторную отправку | Код написан, общая проверка не завершена | Нет |
| Стабильная ссылка на фид | Код написан, реальная проверка с Avito не выполнена | Нет |
| Приватные версионированные XML-файлы | Изолированный экспериментальный код | Нет |
| Безопасное удаление старых XML-файлов | Не реализовано; есть только неисполняемая схема-кандидат | Нет |
| Нормализованная связь задания с запуском фида | Экспериментальный код `0038` | Нет |
| Cleanup служебных исключений | `CODE_READY`: bounded dry-run/apply, статика пройдена; PostgreSQL gate не выполнен | Нет |
| Защита базы `0039` | Не начата | Нет |
| Hardening удаления MarketplaceAccount | Частично записан в `models.py` и `retention.py`, тестирование не завершено | Нет |

## Исторический журнал проверок P0–P6

Раздел ниже сохраняет команды и результаты промежуточных пакетов. Формулировки
`legacy`, `disabled` и старые SHA относятся к моменту соответствующего release,
а не к текущему production-контракту из начала документа.

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

## Исторический account-scoped P6 gate — завершён

Перед fleet-default account `4`, tenant `8` был отдельно проверен через точный
allowlist. Это был временный безопасный этап, а не конечная архитектура SaaS:

```text
MARKETPLACE_FEED_ARTIFACT_MODE=active
MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS=4
MARKETPLACE_FEED_STORAGE_MODE=stable_bridge
MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=false
```

Историческая activation выполнялась exact-confirmed командой
`activate_marketplace_feed_cutover`; до её успешного завершения endpoint
оставался `legacy_bridge`. Неизвестный результат PUT блокировался для ручной
сверки без повторного PUT. Экстренный rollback сначала возвращал endpoint
`private_generation → legacy_bridge`, затем убирал account `4` из allowlist и
возвращал artifact mode в `disabled`. Объекты и audit evidence при этом не
удалялись. PR `#255` закрыл этот gate; PR `#256` затем заменил его общей
fleet-default конфигурацией, указанной в начале документа.

Локальный recovery gate 2026-08-26:

```text
pytest -q \
  apps/marketplaces/tests/test_feed_artifact_canary.py \
  apps/marketplaces/tests/test_feed_artifact_clients.py \
  apps/marketplaces/tests/test_feed_artifact_db_guards.py \
  apps/marketplaces/tests/test_feed_artifact_promotion.py \
  apps/marketplaces/tests/test_feed_artifact_put_reconciliation.py \
  apps/marketplaces/tests/test_feed_artifact_serving.py \
  apps/marketplaces/tests/test_feed_artifact_storage.py \
  apps/marketplaces/tests/test_feed_artifact_upload_ledger.py \
  apps/marketplaces/tests/test_private_feed_recovery_commands.py \
  tests/test_feed_artifact_settings.py \
  tests/test_production_storage_settings.py
результат: 339 passed, 2 skipped in 252.50s

pytest -q \
  apps/marketplaces/tests/test_feed_artifact_clients.py \
  apps/marketplaces/tests/test_feed_artifact_put_reconciliation.py \
  apps/marketplaces/tests/test_feed_artifact_canary.py \
  apps/marketplaces/tests/test_private_feed_recovery_commands.py
результат: 44 passed in 56.03s

flake8 .
результат: exit 0

mypy
результат: Success, 687 source files

mypy --check-untyped-defs --exclude '(^|/)(tests?|migrations)/' \
  apps config backup
результат: Success, 346 source files

python manage.py makemigrations --check --dry-run
результат: No changes detected

python manage.py migrate --noinput
результат: No migrations to apply
```

Два skip — прежние замороженные P7 object-retention/GC integration tests.
Полный backend suite выполняется один раз в CI единственного recovery PR.

### Локальная проверка P5 dual-write production gate

Срез меняет только production validation ingress-режима, pairing-тесты и
документацию. Production environment по умолчанию остаётся `legacy`.

```text
pytest -q tests/test_production_storage_settings.py \
  tests/test_runtime_contract.py
результат: 150 passed in 131.02s

pytest -q
результат: 2357 passed, 1 skipped in 862.98s

flake8 .
mypy
mypy --check-untyped-defs --exclude '(^|/)(tests?|migrations)/' \
  apps config backup
результат: exit 0; 665 и 338 source files соответственно

python manage.py makemigrations --check --dry-run
результат: No changes detected
```

Проверено, что неизвестные ingress-режимы отклоняются, `dual_write` принимается
только вместе с lifecycle `dual_write`, а `legacy` остаётся допустимым rollback.

### Локальная проверка P5 writer/legacy-delivery activation

Финальный срез содержит ровно 20 production-файлов. Он не включает P6/P7,
private storage/serving, cleanup/GC, `0039`, новые миграции или изменение
production feed-флагов.

```text
pytest -q apps/products/tests/test_product_listing_feed_writers.py \
  apps/products/tests/test_admin_feed_safety.py \
  apps/marketplaces/tests/test_admin_safety.py \
  apps/marketplaces/tests/test_feed_intent_local_writers.py \
  apps/products/tests/test_branch_toggle.py \
  apps/products/tests/test_subscription_access_tasks.py \
  apps/image_search/tests apps/marketplaces/tests/test_services.py \
  apps/marketplaces/tests/test_listing_patch_api.py
результат: 208 passed in 75.65s

pytest -q
результат: 2356 passed, 1 skipped in 872.68s

flake8 .
результат: exit 0

mypy
результат: Success, 665 source files

mypy --check-untyped-defs --exclude '(^|/)(tests?|migrations)/' \
  apps config backup
результат: Success, 338 source files

python manage.py makemigrations --check --dry-run
результат: No changes detected

git diff --check
результат: exit 0
```

Проверены отдельно: account-first lock order, stale-generation fencing,
stock-zero как `archiving`, bulk import с одним intent на страницу/аккаунт,
ручные и автоматические изображения, category/address writers, provider
results, malformed report pages, broker failure/repair и закрытие raw writer
путей в Django Admin.

### Локальная проверка безопасного P5 foundation

Фактически оставленный release не меняет legacy coordinator, provider I/O,
товарные/listing writers, private storage, retention, cleanup или GC.
Production отклоняет ingress-режимы кроме `legacy`.

```text
pytest -q apps/marketplaces/tests/test_feed_intents.py \
  apps/marketplaces/tests/test_feed_intent_dispatch.py \
  apps/marketplaces/tests/test_feed_intent_schema.py \
  apps/core/tests/test_feed_intent_recovery.py \
  tests/test_production_storage_settings.py
результат на clean commit: 157 passed in 149.60s

pytest
результат: 2245 passed, 1 skipped in 812.06s

mypy
результат: Success, 654 source files

mypy --check-untyped-defs --exclude '(^|/)(tests?|migrations)/' \
  apps config backup
результат: Success, 336 source files

flake8 .
результат: exit 0

python manage.py makemigrations --check --dry-run
результат: No changes detected

PostgreSQL: marketplaces 0025 -> 0028 -> 0025 -> 0028
результат: 0026, 0027 и 0028 применены, откатились и применились повторно
```

Предварительный полный writer-кандидат дал 149 passed / 28 failed. Неудачные
части не включены: их исправление потребовало бы одновременно менять account
deletion, credential rotation, bulk API, provider-result transitions и legacy
delivery. Это отдельный activation-sensitive пакет, а не допустимый test-fix
для выключенного schema release.

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
deployed exact commit `9061ebb` с выключенной profile migration и
`legacy_public`; активация по-прежнему требует отдельного решения.

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
| P2c tenant status clarity/expiry notices | `ENABLED`, первоначальный PR `#234`; lifecycle сейчас `dual_write` |
| P3 durable feed foundation `0023`–`0024` | `ENABLED`, run сейчас `durable` |
| P4 stable endpoint/profile `0025` | `ENABLED` в штатном onboarding |
| P5 feed-intent foundation/activation `0026`–`0028` | `ENABLED`, ingress сейчас `dual_write` |
| P6 private artifacts и fleet onboarding `0029`–`0031` | `ENABLED`; `0031` отдельно исправляет только live successor guard, без нового режима |
| P7 cleanup/GC/0039 | `FROZEN` |

Физическое разделение не выполняется автоматически: общие файлы
`models.py`, `services.py`, `tasks.py` и settings нужно делить по
отдельным diff-hunks, после чего сразу проверять каждый получившийся пакет.
Точная карта находится в `AVITO_FEED_CHANGESET_MANIFEST.md`.

### Исторический основной acceptance пакета P6

Владелец продукта отдельно разрешил P6 2026-08-25. Недеплоенные миграции
`0029`–`0035` свёрнуты в две новые миграции поверх production `0028`:

- `0029_private_feed_artifacts` — artifact schema и upload/reconciliation
  ledger;
- `0030_private_feed_artifact_guards` — PostgreSQL-защита promotion,
  exact-version serving и rollback.

После production-наблюдения 2026-08-28 отдельно активирована точечная
`0031_live_private_successor_guard`: исходный guard разрешал первую private
generation из legacy bridge, но отвергал следующую immutable generation уже
у живого `private_generation` endpoint при создании PUT ledger и прикреплении
проверенного artifact. Миграция меняет только этот endpoint-state predicate в
обоих guards; все account/tenant/claim/revision, payload, predecessor и
VersionId fences остаются прежними.

Основной пакет merged через PR `#249`; follow-up/recovery PR `#250`–`#254`
устранили реальные несовпадения Avito schema/schedule, Yandex ACL owner и
exact-version response.
Versioned private bucket, отдельный IAM/KMS и presigner настроены. Canary
сохраняет прежний публичный URL фида, атомарно переключает только выбранный
endpoint и имеет rollback обратно на legacy без удаления объекта.

Локально подтверждены:

- полный backend: `2592 passed, 3 skipped` за `635.04s`; три skip —
  integration backup и два намеренно замороженных P7 retention/GC теста;
- focused settings/client/canary/storage: `179 passed`;
- Flake8 по всему repository;
- mypy: `685 source files`, strict mypy: `345 source files`;
- migration drift: `No changes detected`; OpenAPI validation без warnings;
- чистое применение `0029`–`0030` и rollback/reapply `0030 → 0028 → 0030`
  на отдельной свежей PostgreSQL-базе;
- потоковая генерация 10 000 объявлений в установленном лимите времени,
  диска и памяти (`29.01s` в локальном Docker gate);
- end-to-end PostgreSQL canary: STOP для пустой публикации, exact-version
  upload/readback, атомарное promotion и legacy rollback.

Cloud preflight проверяет owner contract через `GetBucketAcl`, включённое
versioning и точный default KMS key до любого canary PUT. Yandex может опустить
оба owner-поля; частичный или другой owner блокируется.

Canary/recovery, постоянный account `4` cutover и fleet-default последовательно
завершены PR `#253`–`#256`. Текущая конфигурация и release evidence находятся
в начале документа. P7, retention delete, GC, удаление объектов и `0039`
по-прежнему не входят в P6.
