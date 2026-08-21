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

Локальный gate закрыт: 24 status-fencing, 302 Marketplace и 1 947 backend
tests; frontend typecheck/ESLint/25 unit tests/production build; migration
drift, OpenAPI, flake8 и mypy. Deploy возможен только в режиме `legacy` после
отдельного PR/CI.

## P3 — надёжный запуск фида, миграции 0023–0024

Основные файлы:

- migrations 0023–0024;
- feed_workflow.py;
- reconcile_marketplace_feed_run.py;
- feed run/workflow/recovery/payload tests;
- только feed-run части models/tasks/services/retention/admin;
- billing/plan части только при необходимости предела 10 000 объявлений.

Не включать stable endpoint и private storage.

Узкая проверка:

- test_feed_run_schema.py;
- test_feed_workflow.py;
- test_reconcile_marketplace_feed_run.py;
- test_durable_feed_payload_limit.py;
- test_durable_feed_provenance.py;
- test_durable_feed_tasks.py.

Deploy: код и схема выключены; production run mode остаётся legacy.

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

## P5 — feed intents и восстановление legacy, миграции 0026–0031

Основные файлы:

- docs/AVITO_FEED_LEGACY_RECOVERY.md;
- migrations 0026–0031;
- feed_intents.py, feed_cursor_reconciliation.py,
  feed_report_reconciler.py;
- reconcile_legacy_feed_cursor.py;
- intent/dispatch/legacy-repair/provider-result tests;
- writer-части marketplace/products/image_search/media_processing/web_research;
- связанные части core dispatch/retention и notifications;
- ingress settings без включения private artifacts.

Не включать upload ledger 0032+, GC или dispatch fence.

Узкая проверка:

- test_feed_intents.py;
- test_feed_intent_dispatch.py;
- test_feed_flush_delivery_repair.py;
- test_provider_result_feed_intents.py;
- test_feed_intent_local_writers.py;
- test_local_lifecycle_writers.py;
- test_reconcile_legacy_feed_cursor.py;
- product/image writer tests.

Deploy: сначала только legacy. Dual-write — отдельный canary после schema deploy.

## P6 — private artifact experiment, миграции 0032–0035

Основные файлы:

- migrations 0032–0035;
- feed_artifact_storage.py, feed_artifact_promotion.py,
  feed_artifact_serving.py, feed_artifact_put_reconciliation.py;
- streaming writer/deterministic OEM части Avito feed builder;
- только artifact settings/admin/model/retention части;
- artifact upload/serving/promotion/reconciliation tests.

Deploy: не входит в ближайший production release. Сначала отдельное решение,
реальный bucket/IAM/KMS canary и нагрузочный тест.

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
6. запуск с production-like legacy settings;
7. проверка rollback до предыдущего пакета;
8. подтверждение, что deploy environment не меняет feed-флаги.
