import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


MIGRATE_FROM = [
    ('products', '0040_productparsejob_fallback_origin_key'),
    ('tenants', '0016_webhook_delivery_claim_constraint'),
    ('web_research', '0004_provider_outcome_evidence'),
]
MIGRATE_TO = [('web_research', '0008_constrain_search_attempt_ledger')]


def _restore_leaf_migrations():
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_search_workflow_migration_handles_empty_legacy_ledger(transactional_db):
    assert connection.in_atomic_block is False
    executor = MigrationExecutor(connection)
    executor.migrate(MIGRATE_FROM)
    try:
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_TO)
        apps = executor.loader.project_state(MIGRATE_TO).apps
        assert apps.get_model(
            'web_research', 'WebSearchWorkflow',
        ).objects.count() == 0
    finally:
        _restore_leaf_migrations()


@pytest.mark.django_db(transaction=True)
def test_search_workflow_migration_groups_duplicate_unknown_legacy_calls(
    transactional_db,
):
    assert connection.in_atomic_block is False
    executor = MigrationExecutor(connection)
    executor.migrate(MIGRATE_FROM)
    old_apps = executor.loader.project_state(MIGRATE_FROM).apps
    Tenant = old_apps.get_model('tenants', 'Tenant')
    Product = old_apps.get_model('products', 'Product')
    Run = old_apps.get_model('web_research', 'WebResearchRun')
    Attempt = old_apps.get_model('web_research', 'WebSearchAttempt')
    tenant = Tenant.objects.create(name='Legacy web ledger', slug='legacy-web-ledger')
    product = Product.objects.create(
        tenant=tenant,
        article='LEGACY-WEB',
        name='Legacy web result',
        price='1.00',
    )
    first_run = Run.objects.create(
        tenant=tenant, product=product, status='failed',
    )
    second_run = Run.objects.create(
        tenant=tenant, product=product, status='failed',
    )
    attempt_ids = [
        Attempt.objects.create(
            run=legacy_run,
            provider_id=provider_id,
            query='same paid business domain',
            status='outcome_uncertain',
        ).pk
        for legacy_run, provider_id in (
            (first_run, 'brave'),
            (second_run, 'tavily'),
        )
    ]

    try:
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_TO)
        apps = executor.loader.project_state(MIGRATE_TO).apps
        Workflow = apps.get_model('web_research', 'WebSearchWorkflow')
        MigratedAttempt = apps.get_model('web_research', 'WebSearchAttempt')
        workflows = list(Workflow.objects.filter(status='uncertain'))
        migrated = list(MigratedAttempt.objects.filter(
            pk__in=attempt_ids,
        ).order_by('pk'))

        assert len(workflows) == 1
        assert {attempt.workflow_id for attempt in migrated} == {workflows[0].pk}
        assert workflows[0].domain_reference == (
            f'product:{product.pk}:purpose:enrichment'
        )
        assert {
            attempt.domain_reference for attempt in migrated
        } == {workflows[0].domain_reference}
        assert all(attempt.apply_state == 'pending' for attempt in migrated)
        assert len({attempt.call_key for attempt in migrated}) == 2
    finally:
        _restore_leaf_migrations()
