# Точный inventory замороженного Avito feed WIP

Обновлено: 2026-08-20.

Это неизменяемый исторический inventory исходного snapshot, а не карта
текущего `main`. Метки `P6 FROZEN` ниже означают состояние и владельца hunk на
2026-08-20; P6 впоследствии был независимо выделен, проверен и включён через
PR `#249`–`#256`. Метки P7 по-прежнему заморожены. Актуальный runtime описан в
[`AVITO_FEED_STATUS.md`](AVITO_FEED_STATUS.md).

Исходный snapshot: `8ae8e265656dcae00a96111ac6477bb1d05e7e8f` относительно
base `7415ccca0ae54fccc9cb389704fa8e183feea213`. Он содержит 104 добавленных
и 65 изменённых путей. `.claude/settings.local.json` исключён до создания
snapshot и не относится ни к одному пакету.

`SHARED HUNKS` — не пакет: эти файлы делятся по diff hunk только после
отдельной активации пакета-владельца. Исторические метки `P6 FROZEN`, текущие
`P7 FROZEN` и `OUTSIDE P0-P7` нельзя механически переносить из snapshot.
Незавершённый hardening удаления MarketplaceAccount в `models.py` и
`retention.py` остаётся непроверенным внутри `SHARED HUNKS` и не входит в P0.

| Статус snapshot | Путь | Владелец |
|---|---|---|
| M | `.env.example` | SHARED HUNKS |
| M | `.github/workflows/production-monitor.yml` | P1 |
| A | `AGENTS.md` | P0 |
| M | `README.md` | P0 SELECTED HUNKS |
| M | `apps/billing/management/commands/seed_plans.py` | P3 |
| M | `apps/billing/tests/test_seed_plans.py` | P3 |
| M | `apps/billing/tests/test_services.py` | P3 |
| M | `apps/core/admin_views.py` | P1 |
| M | `apps/core/apps.py` | P1 |
| A | `apps/core/celery_observability.py` | P1 |
| M | `apps/core/dispatch.py` | SHARED HUNKS |
| M | `apps/core/management/commands/restore_soft_deleted.py` | SHARED HUNKS |
| M | `apps/core/management/commands/setup_periodic_tasks.py` | SHARED HUNKS |
| M | `apps/core/middleware.py` | P4 |
| A | `apps/core/queue_observability.py` | P1 |
| M | `apps/core/retention.py` | SHARED HUNKS |
| M | `apps/core/tasks.py` | SHARED HUNKS |
| A | `apps/core/telemetry.py` | P1 |
| A | `apps/core/tests/test_celery_observability.py` | P1 |
| A | `apps/core/tests/test_feed_intent_recovery.py` | P5 |
| A | `apps/core/tests/test_observability_periodic.py` | P1 |
| A | `apps/core/tests/test_queue_observability.py` | P1 |
| A | `apps/core/tests/test_restore_soft_deleted_safety.py` | SHARED HUNKS |
| M | `apps/core/tests/test_retention.py` | SHARED HUNKS |
| M | `apps/core/tests/test_sentry_scrubbing.py` | P1 |
| M | `apps/datasources/models.py` | P5 |
| M | `apps/datasources/views.py` | P5 |
| M | `apps/image_search/services/moderation.py` | P5 |
| M | `apps/image_search/services/pipeline.py` | P5 |
| M | `apps/image_search/views.py` | P5 |
| M | `apps/marketplaces/adapters/avito/adapter.py` | SHARED HUNKS |
| M | `apps/marketplaces/adapters/avito/feed_builder.py` | SHARED HUNKS |
| A | `apps/marketplaces/adapters/avito/profile_migration.py` | P4 |
| M | `apps/marketplaces/adapters/avito/rate_limiter.py` | SHARED HUNKS |
| M | `apps/marketplaces/admin.py` | SHARED HUNKS |
| M | `apps/marketplaces/avito_tree_import.py` | P5 |
| A | `apps/marketplaces/feed_artifact_promotion.py` | P6 FROZEN |
| A | `apps/marketplaces/feed_artifact_put_reconciliation.py` | P6 FROZEN |
| A | `apps/marketplaces/feed_artifact_serving.py` | P6 FROZEN |
| A | `apps/marketplaces/feed_artifact_storage.py` | P6 FROZEN |
| A | `apps/marketplaces/feed_cursor_reconciliation.py` | P5 |
| A | `apps/marketplaces/feed_endpoint.py` | P4 |
| A | `apps/marketplaces/feed_endpoint_views.py` | P4 |
| A | `apps/marketplaces/feed_intents.py` | P5 |
| A | `apps/marketplaces/feed_profile_migration.py` | P4 |
| A | `apps/marketplaces/feed_report_reconciler.py` | P5 |
| A | `apps/marketplaces/feed_run_dispatch_backfill.py` | P7 FROZEN |
| A | `apps/marketplaces/feed_run_dispatch_fence.py` | P7 FROZEN |
| A | `apps/marketplaces/feed_run_dispatch_terminal_cleanup.py` | P7 FROZEN |
| A | `apps/marketplaces/feed_workflow.py` | P3 |
| A | `apps/marketplaces/listing_lifecycle.py` | P2 |
| A | `apps/marketplaces/management/commands/backfill_listing_status_lifecycle.py` | P2 |
| A | `apps/marketplaces/management/commands/backfill_marketplace_feed_run_dispatch_fences.py` | P7 FROZEN |
| A | `apps/marketplaces/management/commands/cleanup_marketplace_feed_run_dispatch_terminal_exceptions.py` | P7 FROZEN |
| A | `apps/marketplaces/management/commands/migrate_marketplace_feed_profile.py` | P4 |
| A | `apps/marketplaces/management/commands/reconcile_legacy_feed_cursor.py` | P5 |
| A | `apps/marketplaces/management/commands/reconcile_marketplace_feed_run.py` | P3 |
| A | `apps/marketplaces/migrations/0020_listing_status_lifecycle_expand.py` | P2 |
| A | `apps/marketplaces/migrations/0021_status_lifecycle_concurrent_indexes.py` | P2 |
| A | `apps/marketplaces/migrations/0022_account_status_lifecycle_concurrent_index.py` | P2 |
| A | `apps/marketplaces/migrations/0023_marketplace_feed_run.py` | P3 |
| A | `apps/marketplaces/migrations/0024_feed_run_listing_concurrent_index.py` | P3 |
| A | `apps/marketplaces/migrations/0025_marketplace_feed_endpoint.py` | P4 |
| A | `apps/marketplaces/migrations/0026_feed_intent_expand.py` | P5 |
| A | `apps/marketplaces/migrations/0027_feed_intent_due_concurrent_index.py` | P5 |
| A | `apps/marketplaces/migrations/0028_feed_run_source_intent_unique.py` | P5 |
| A | `apps/marketplaces/migrations/0029_feed_endpoint_artifact_concurrent_index.py` | P6 FROZEN |
| A | `apps/marketplaces/migrations/0030_feed_run_artifact_concurrent_index.py` | P6 FROZEN |
| A | `apps/marketplaces/migrations/0031_feed_artifact_ownership_guards.py` | P6 FROZEN |
| A | `apps/marketplaces/migrations/0032_feed_artifact_upload_attempt.py` | P6 FROZEN |
| A | `apps/marketplaces/migrations/0033_feed_artifact_upload_guards.py` | P6 FROZEN |
| A | `apps/marketplaces/migrations/0034_feed_put_reconciliation_audit.py` | P6 FROZEN |
| A | `apps/marketplaces/migrations/0035_feed_put_reconciliation_audit_guards.py` | P6 FROZEN |
| A | `apps/marketplaces/migrations/0036_feed_artifact_gc_intent.py` | P7 FROZEN |
| A | `apps/marketplaces/migrations/0037_feed_artifact_gc_intent_guards.py` | P7 FROZEN |
| A | `apps/marketplaces/migrations/0038_feed_run_dispatch_fence.py` | P7 FROZEN |
| M | `apps/marketplaces/models.py` | SHARED HUNKS |
| M | `apps/marketplaces/services.py` | SHARED HUNKS |
| M | `apps/marketplaces/tasks.py` | SHARED HUNKS |
| M | `apps/marketplaces/tests/test_account_api.py` | P4 |
| A | `apps/marketplaces/tests/test_admin_safety.py` | SHARED HUNKS |
| M | `apps/marketplaces/tests/test_avito.py` | SHARED HUNKS |
| M | `apps/marketplaces/tests/test_avito_account_status.py` | SHARED HUNKS |
| A | `apps/marketplaces/tests/test_avito_feed_builder_scaling.py` | P6 FROZEN |
| A | `apps/marketplaces/tests/test_avito_feed_stream_writer.py` | P6 FROZEN |
| M | `apps/marketplaces/tests/test_avito_tree_import.py` | P5 |
| A | `apps/marketplaces/tests/test_backfill_listing_status_lifecycle.py` | P2 |
| A | `apps/marketplaces/tests/test_durable_feed_payload_limit.py` | P3 |
| A | `apps/marketplaces/tests/test_durable_feed_provenance.py` | P3 |
| A | `apps/marketplaces/tests/test_durable_feed_tasks.py` | P3 |
| A | `apps/marketplaces/tests/test_feed_artifact_db_guards.py` | P6 FROZEN |
| A | `apps/marketplaces/tests/test_feed_artifact_gc_intent_schema.py` | P7 FROZEN |
| A | `apps/marketplaces/tests/test_feed_artifact_promotion.py` | P6 FROZEN |
| A | `apps/marketplaces/tests/test_feed_artifact_put_reconciliation.py` | P6 FROZEN |
| A | `apps/marketplaces/tests/test_feed_artifact_serving.py` | P6 FROZEN |
| A | `apps/marketplaces/tests/test_feed_artifact_storage.py` | P6 FROZEN |
| A | `apps/marketplaces/tests/test_feed_artifact_upload_ledger.py` | P6 FROZEN |
| A | `apps/marketplaces/tests/test_feed_dark_schema.py` | P5 |
| A | `apps/marketplaces/tests/test_feed_endpoint_route.py` | P4 |
| A | `apps/marketplaces/tests/test_feed_endpoint_schema.py` | P4 |
| A | `apps/marketplaces/tests/test_feed_flush_delivery_repair.py` | P5 |
| A | `apps/marketplaces/tests/test_feed_integration_hardening.py` | SHARED HUNKS |
| A | `apps/marketplaces/tests/test_feed_intent_dispatch.py` | P5 |
| A | `apps/marketplaces/tests/test_feed_intent_local_writers.py` | P5 |
| A | `apps/marketplaces/tests/test_feed_intents.py` | P5 |
| A | `apps/marketplaces/tests/test_feed_profile_migration.py` | P4 |
| A | `apps/marketplaces/tests/test_feed_put_reconciliation_audit_schema.py` | P6 FROZEN |
| A | `apps/marketplaces/tests/test_feed_report_reconciler.py` | P5 |
| A | `apps/marketplaces/tests/test_feed_run_dispatch_backfill.py` | P7 FROZEN |
| A | `apps/marketplaces/tests/test_feed_run_dispatch_fence_runtime.py` | P7 FROZEN |
| A | `apps/marketplaces/tests/test_feed_run_dispatch_fence_schema.py` | P7 FROZEN |
| A | `apps/marketplaces/tests/test_feed_run_dispatch_terminal_cleanup.py` | P7 FROZEN |
| A | `apps/marketplaces/tests/test_feed_run_schema.py` | P3 |
| A | `apps/marketplaces/tests/test_feed_workflow.py` | P3 |
| A | `apps/marketplaces/tests/test_listing_lifecycle.py` | P2 |
| A | `apps/marketplaces/tests/test_local_lifecycle_writers.py` | P5 |
| A | `apps/marketplaces/tests/test_migrate_marketplace_feed_profile_command.py` | P4 |
| A | `apps/marketplaces/tests/test_provider_result_feed_intents.py` | P5 |
| A | `apps/marketplaces/tests/test_reconcile_legacy_feed_cursor.py` | P5 |
| A | `apps/marketplaces/tests/test_reconcile_marketplace_feed_run.py` | P3 |
| A | `apps/marketplaces/tests/test_status_fencing.py` | P2 |
| A | `apps/marketplaces/tests/test_status_lifecycle_expand.py` | P2 |
| M | `apps/marketplaces/views.py` | SHARED HUNKS |
| M | `apps/media_processing/services.py` | P5 |
| M | `apps/notifications/tasks.py` | P5 |
| M | `apps/notifications/tests/test_notifications.py` | P5 |
| M | `apps/products/admin.py` | P5 |
| A | `apps/products/feed_writers.py` | P5 |
| M | `apps/products/management/commands/dedupe_auto_parts_categories.py` | P5 |
| M | `apps/products/models.py` | P5 |
| M | `apps/products/services.py` | P5 |
| M | `apps/products/storage.py` | P5 |
| M | `apps/products/tasks.py` | SHARED HUNKS |
| A | `apps/products/tests/test_admin_feed_safety.py` | SHARED HUNKS |
| A | `apps/products/tests/test_automatic_image_feed_writer.py` | P5 |
| M | `apps/products/tests/test_dedupe_auto_parts_categories.py` | P5 |
| A | `apps/products/tests/test_feed_intent_writers.py` | P5 |
| M | `apps/products/tests/test_subscription_access_tasks.py` | SHARED HUNKS |
| M | `apps/products/views.py` | P5 |
| M | `apps/sync/admin.py` | P5 |
| M | `apps/sync/tasks.py` | P5 |
| A | `apps/sync/tests/test_admin.py` | SHARED HUNKS |
| M | `apps/tenants/admin.py` | SHARED HUNKS |
| M | `apps/tenants/models.py` | SHARED HUNKS |
| A | `apps/web_research/tests/test_feed_writer_lock_order.py` | P5 |
| M | `apps/web_research/views.py` | P5 |
| M | `config/sentry_scrubbing.py` | P1 |
| M | `config/settings/base.py` | SHARED HUNKS |
| M | `config/settings/production.py` | SHARED HUNKS |
| M | `config/urls.py` | P4 |
| A | `docs/AVITO_FEED_CHANGESET_MANIFEST.md` | P0 |
| A | `docs/AVITO_FEED_LEGACY_RECOVERY.md` | P5 |
| A | `docs/AVITO_FEED_ROADMAP.md` | P0 |
| A | `docs/AVITO_FEED_STATUS.md` | P0 |
| M | `docs/DEPLOYMENT.md` | P0 SELECTED HUNKS |
| A | `docs/ENGINEERING_EXECUTION_RULES.md` | P0 |
| A | `docs/OBSERVABILITY.md` | P1 |
| M | `docs/PRODUCTION_SECURITY.md` | P0 SELECTED HUNKS |
| M | `docs/RELEASE_CHECKLIST.md` | P0 SELECTED HUNKS |
| A | `docs/SCALING_AND_MARKETPLACE_EVOLUTION_PLAN.md` | OUTSIDE P0-P7 |
| M | `frontend/src/app/dashboard/settings/page.tsx` | P4 |
| M | `nginx.conf` | P4 |
| M | `templates/admin/stats.html` | P1 |
| M | `tests/test_avito_rate_limit_contract.py` | P4 |
| A | `tests/test_feed_artifact_settings.py` | P6 FROZEN |
| A | `tests/test_feed_ingress_settings.py` | P5 |
| M | `tests/test_production_host_contract.py` | P1 |
| M | `tests/test_production_storage_settings.py` | SHARED HUNKS |
| M | `tests/test_runtime_contract.py` | SHARED HUNKS |
