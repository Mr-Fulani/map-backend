# Карта разделения текущих изменений

Этот файл переводит roadmap в практические границы commit/PR. Он не разрешает
разработку новых функций. Порядок пакетов задаёт
[AVITO_FEED_ROADMAP.md](AVITO_FEED_ROADMAP.md).

Полный построчный учёт всех 169 путей frozen snapshot находится в
[`AVITO_FEED_SNAPSHOT_INVENTORY.md`](AVITO_FEED_SNAPSHOT_INVENTORY.md).

## Важное ограничение

Файлы apps/marketplaces/models.py, services.py, tasks.py, admin.py,
config/settings/*.py и apps/core/retention.py содержат изменения нескольких
пакетов. Их нельзя переносить целиком. Они разделяются по отдельным частям
diff, и после каждого переноса пакет обязан самостоятельно импортироваться и
проходить тесты.

Нельзя механически разделить только новые файлы, оставив весь diff общих файлов
в последнем PR: ранние пакеты тогда не будут рабочими.

## P0 — документация и freeze

Точный состав текущего P0:

- AGENTS.md;
- README.md;
- docs/AVITO_FEED_CHANGESET_MANIFEST.md;
- docs/AVITO_FEED_ROADMAP.md;
- docs/AVITO_FEED_SNAPSHOT_INVENTORY.md;
- docs/AVITO_FEED_STATUS.md;
- docs/ENGINEERING_EXECUTION_RULES.md;
- только freeze-ссылки и legacy-контракт в DEPLOYMENT.md,
  PRODUCTION_SECURITY.md и RELEASE_CHECKLIST.md.

Не включать `.claude/settings.local.json`, runtime-код, настройки приложения,
миграции и тесты будущих пакетов. `AVITO_FEED_LEGACY_RECOVERY.md` относится к
P5, `OBSERVABILITY.md` — к P1, а общий
`SCALING_AND_MARKETPLACE_EVOLUTION_PLAN.md` остаётся только в исходном
not-for-merge snapshot до отдельного review его нефидовых утверждений.

Проверка: git diff --check. Этот пакет не меняет приложение.

## P1 — observability и production checks

Фактически выделенный состав P1:

- `.github/workflows/production-monitor.yml`;
- `apps/core/admin_views.py`, `apps/core/apps.py`;
- `apps/core/celery_observability.py`, `apps/core/queue_observability.py`,
  `apps/core/telemetry.py`;
- `apps/core/tests/test_celery_observability.py`,
  `test_observability_periodic.py`, `test_queue_observability.py` и
  `test_sentry_scrubbing.py`;
- `config/sentry_scrubbing.py`, `docs/OBSERVABILITY.md` и
  `templates/admin/stats.html`;
- `tests/test_production_host_contract.py`;
- только observability-hunks в `.env.example`,
  `apps/core/management/commands/setup_periodic_tasks.py`,
  `apps/core/tasks.py`, marketplace Avito adapter/rate limiter и его тесте,
  datasource-import task и его тесте, `config/settings/base.py`,
  `config/settings/production.py` и `tests/test_runtime_contract.py`.

После hunk-аудита из P1 исключены ошибочно назначенные целиком файлы:

- public feed path в `apps/core/middleware.py` относится к P4;
- datasource polling guards в `apps/datasources/models.py` и `views.py`
  относятся к P5;
- stable endpoint assertions в `tests/test_avito_rate_limit_contract.py`
  относятся к P4;
- `feed_poll` budget и прочие будущие feed-hunks не перенесены.

Не включать feed migrations, lifecycle/product writer changes, stable endpoint,
private storage, cleanup, GC или worker activation будущих пакетов.

Узкая проверка: observability, Sentry, production-host и runtime-contract tests.
Deploy: отдельный релиз без изменения feed-флагов.

## P2 — lifecycle объявлений, последовательные release

Три миграции нельзя выпускать одним пакетом из-за общего ограничения не более
двух миграций. Поэтому P2 физически разделён на P2a и P2b. P2b дополнительно
разделён на P2b1/P2b2, чтобы не превышать 1 500 production-строк и не смешивать
подготовку данных с runtime fencing.

### P2a — additive schema, миграции 0020–0021

Точный состав:

- только lifecycle-field hunks в `apps/marketplaces/models.py`;
- `0020_listing_status_lifecycle_expand.py`;
- `0021_status_lifecycle_concurrent_indexes.py` только с listing due index;
- `test_status_lifecycle_expand.py` со schema, PostgreSQL catalog,
  existing-row upgrade и rollback contracts;
- status/roadmap/manifest documentation.

Не включать `0022`, lifecycle service, backfill command, services/tasks/views,
admin, settings, scheduler, MarketplaceFeedRun, stable endpoint, feed intents,
private artifacts, cleanup или GC.

Deploy: только additive schema. Production продолжает работать старым кодом;
новые поля nullable и не читаются runtime-логикой.

### P2b1 — lifecycle/index/backfill, миграция 0022 — deployed

Точный состав:

- migration 0022 с account due index;
- `apps/marketplaces/listing_lifecycle.py`;
- `backfill_listing_status_lifecycle.py`;
- только account-index hunk в marketplace model;
- только `AVITO_STATUS_LIFECYCLE_MODE` в env/base/production settings;
- lifecycle/backfill/production-settings tests;
- status/roadmap/manifest documentation.

Не включать marketplace services/tasks/views/admin, scheduler,
`test_status_fencing.py`, MarketplaceFeedRun, stable endpoint, feed intents и
artifacts.

Узкая проверка:

- test_listing_lifecycle.py;
- test_backfill_listing_status_lifecycle.py.
- test_production_storage_settings.py lifecycle cases;
- PostgreSQL upgrade/rollback/catalog migration contracts.

Deploy: production commit `de0d202`, exact setting `legacy`, ручной backfill
apply не запускался, новый scheduler отсутствует. PR/CI/deploy/observation gate
закрыт 2026-08-21.

### P2b2 — runtime fencing/dual-write, без новой миграции

Точный состав формируется отдельным hunk-review:

- только lifecycle/fencing части marketplace services/tasks;
- `test_status_fencing.py`;
- без scheduler activation, views/admin, feed-run, stable endpoint, intents,
  artifacts, cleanup и GC.

Deploy: status mode по-прежнему `legacy`, scheduler выключен. Gate P2b1
закрыт; P2b2 активирован пользователем 2026-08-21. Локальный P2b2 gate
закрыт: 15 status-fencing, 292 Marketplace и 1 937 backend-тестов прошли;
новых миграций нет. PR `#232` merged в `0ef04de`; PR/main CI, manual production
monitor и десятиминутное наблюдение зелёные. Production остаётся `legacy`,
lifecycle scheduler не активирован.

### P2c — tenant visibility/expiry notices, без новой миграции

Точный состав:

- только tenant-notice hunk в `apps/marketplaces/tasks.py`;
- только `last_sync_at` в marketplace serializers;
- dashboard/listing/drawer подписи времени последней provider-проверки;
- status-fencing и serializer regressions;
- status/roadmap/manifest documentation.

Не включать status transitions, новые lifecycle/feed modes, scheduler, новые
очереди, migrations, feed-run, stable endpoint, private artifacts, cleanup или
GC. `finish_time` берётся только из уже выполняемого provider GET; фиксированный
месячный срок не предполагается.

Локальный gate закрыт: 25 status-fencing, 303 Marketplace и 1 948 backend
tests; frontend typecheck/ESLint/25 unit tests/production build; migration
drift, OpenAPI, flake8 и mypy. PR `#234` merged в `1f05367`; PR/main CI,
production backup/readiness/topology и manual monitor зелёные. Production остаётся
`legacy`; десятиминутное наблюдение зелёное, новых
migrations/scheduler/queue/settings нет.

## P3 — надёжный запуск фида, миграции 0023–0024

Основные файлы:

- migrations 0023–0024;
- feed_workflow.py;
- reconcile_marketplace_feed_run.py;
- feed run/workflow/recovery/payload tests;
- только feed-run части models/tasks/admin;
- billing/plan части только при необходимости предела 10 000 объявлений.

Не включать stable endpoint и private storage.
Не включать retention-удаление, отвязывание feed run или GC: история P3
сохраняется до отдельной активации P7.

Узкая проверка:

- test_feed_run_schema.py;
- test_feed_workflow.py;
- test_reconcile_marketplace_feed_run.py;
- test_durable_feed_payload_limit.py;
- test_durable_feed_provenance.py;
- test_durable_feed_tasks.py.

Deploy: PR `#244`, production commit `f1881f1`; код и схема выключены,
production run mode остаётся legacy.

## P4 — stable endpoint и профиль Autoload, миграция 0025

Основные файлы:

- migration 0025;
- feed_endpoint.py, feed_endpoint_views.py, feed_profile_migration.py;
- adapters/avito/profile_migration.py;
- migrate_marketplace_feed_profile.py;
- endpoint/profile/route tests;
- только связанные URL/Nginx/frontend/settings/service/account-guard части.

Не включать private artifact storage и migrations 0026+.

Deploy: profile migration false, storage legacy_public. Включение требует
отдельного реального Avito 307 canary.

Фактически выделенный P4 не содержит migrations `0026+`, ingress/feed-intent
hunks, private artifacts, cleanup/GC или worker wiring. Локально прошли 105
узких тестов, полный backend (`2198 passed, 1 skipped`), clean и P3-upgrade
PostgreSQL, rollback/reapply `0025`, migration drift, flake8, frontend
ESLint/typecheck, оба mypy gate и `git diff --check`. PR `#245` merged и
deployed exact commit `9061ebb`; P4 остаётся выключенным.

## P5 — безопасный feed-intent foundation, миграции 0026–0028

Основные файлы:

- migrations 0026–0028;
- feed_intents.py;
- exact-revision dark scanner/worker и terminal dispatch recovery;
- только intent/schema/dispatch/recovery/production-settings tests;
- ingress setting, жёстко зафиксированный в production на `legacy`.

Не включать writer cutover, legacy flush replacement, report reconciler,
private artifact schema/storage, upload ledger, GC или dispatch fence.

Узкая проверка:

- test_feed_intents.py;
- test_feed_intent_dispatch.py;
- test_feed_intent_schema.py;
- test_feed_intent_recovery.py;
- production legacy-only settings contract.

Deploy: только legacy. В этом release production-конфигурация отклоняет
dual-write/durable; следующий activation package требует отдельного решения.

Foundation merged через PR `#246` и deployed exact commit `2e9958c` с
неизменным legacy runtime.

### P5 activation — writer fencing и legacy delivery repair

Разрешённый production-срез (ровно 20 файлов):

- `apps/image_search/services/moderation.py`, `pipeline.py`, `views.py`;
- `apps/media_processing/services.py`;
- `apps/products/admin.py`, `feed_writers.py`, `models.py`, `services.py`,
  `storage.py`, `tasks.py`, `views.py`;
- `apps/marketplaces/adapters/avito/adapter.py`;
- `apps/marketplaces/admin.py`, `feed_report_reconciler.py`, `models.py`,
  `services.py`, `tasks.py`, `views.py`;
- `apps/tenants/admin.py`, `models.py`.

Обязательные тестовые срезы:

- feed intent local writers, flush delivery repair, report reconciler и
  provider-result intents;
- product/image writers, product-to-listing fencing и admin safety;
- существующие Avito, status fencing, branch toggle, datasource import,
  image API, marketplace service/API regressions;
- полный backend suite.

Не включать `feed_cursor_reconciliation.py`, P6/P7 artifact/cleanup/GC,
миграции `0029+`, datasource polling refactor или изменение production
settings. Deploy кода выполняется только с прежними legacy-флагами;
`dual_write` — отдельный rollout после release gate.

## P6 — private artifacts и fleet onboarding, миграции 0029–0030 — завершён

Основные файлы:

- `0029_private_feed_artifacts` и `0030_private_feed_artifact_guards`,
  свёрнутые поверх production `0028`;
- feed_artifact_storage.py, feed_artifact_promotion.py,
  feed_artifact_serving.py, feed_artifact_put_reconciliation.py;
- feed_artifact_clients.py, feed_artifact_canary.py и ручная bounded
  `canary_private_feed_artifact` management-команда;
- account-scoped `reconcile_private_feed_artifact_put` и exact-fenced
  `canary_private_feed_artifact --phase resume` только для recovery уже
  существующей `put_pending` attempt;
- fail-closed `feed_cutover.py`, exact-one production allowlist и
  `activate_marketplace_feed_cutover` для отдельно разрешённого постоянного
  cutover account `4`;
- account-scoped private durable generation в существующем P5 intent worker:
  streaming XML, one-shot PUT, exact-version readback, atomic endpoint
  successor promotion, Avito trigger и polling;
- `P6_PRIVATE_FEED_CANARY_RUNBOOK.md` с exact rollback и fail-closed
  неизвестного PUT;
- streaming writer/deterministic OEM части Avito feed builder;
- только artifact settings/admin/model части; retention delete не входит;
- artifact upload/serving/promotion/reconciliation tests.

P6 разрешён 2026-08-25. Deploy выполнялся последовательно: выключенный release,
ручной canary/recovery одного Avito-аккаунта, постоянный account-scoped cutover
и fleet-default. Любые object delete/GC остаются вне пакета.

Локальный основной acceptance закрыт: `2592 passed, 3 skipped`, flake8, оба
mypy, migration drift, OpenAPI, свежие migrations `0029`–`0030` и их точный
rollback/reapply зелёные. PR `#249`–`#252`, выключенный deploy и cloud
preflight завершены на production `5ad92ad`. Первый account 4 canary оставил
одну fail-closed `put_pending` attempt без endpoint promotion. Recovery для
неё отдельно разрешён одним PR: read-only exact-version list, immutable audit,
resume только attempt N+1 и rollback в `disabled/stable_bridge`; никаких
delete/GC/P7/0039/new mode/worker wiring в него не входит.

Recovery PR `#253`–`#254` выложен на production `827040c`, PR `#255` завершил
private cutover account `4` на `139ed48`, а PR `#256` завершил fleet-default
на production `0762ab5`.

Фактически выпущенный 2026-08-27 P6 fleet-default пакет включает только:

- fleet admission при `durable/dual_write/dual_write`, `active/stable_bridge`,
  пустом cutover allowlist и выключенной profile migration;
- синхронное резервирование endpoint при создании аккаунта;
- tenant-scoped, сериализованный и fail-closed Autoload profile onboarding;
- publication hold без legacy upload до подтверждения managed endpoint;
- lifecycle-aware dashboard status и managed-URL подсказки frontend;
- production settings, regression tests и документацию.

Пакет не добавил миграции, новые режимы, periodic worker, sweep старых
аккаунтов, object delete, GC, P7 или `0039`. Полный PR CI `33018719809`
успешен. Merge tree совпал с проверенным head tree; дублирующий push-main CI
был отменён только после этой проверки. Exact deploy/health evidence находится
в [`AVITO_FEED_STATUS.md`](AVITO_FEED_STATUS.md).

## P7 — замороженный backlog, миграции 0036–0039

Сюда относятся:

- migrations 0036–0038 и будущая 0039;
- feed_run_dispatch_fence.py и feed_run_dispatch_backfill.py;
- `CODE_READY`, но не `VERIFIED` feed_run_dispatch_terminal_cleanup.py и
  отдельная management-команда;
- GC intent, dispatch fence, backfill/cleanup tests;
- будущие object delete, detach и retention cutover.

Эти файлы не входят в P1–P6 и сейчас не deploy-ятся. Cleanup имеет только
статически проверенный `CODE_READY` статус; PostgreSQL/full-suite acceptance и
production dry-run не выполнены. Он сохраняется в P7 для отдельного решения
после staging P6. Auto-applied `0039` не включается в один release с cleanup.

## Общая проверка перед каждым merge

После узкого набора обязательны:

1. makemigrations --check --dry-run;
2. миграции на чистой PostgreSQL;
3. миграции копии схемы предыдущего пакета;
4. полный backend pytest;
5. flake8 и git diff --check;
6. запуск с production-like настройками текущего или целевого этапа;
7. проверка rollback до предыдущего пакета;
8. подтверждение точных feed-флагов после deploy.
