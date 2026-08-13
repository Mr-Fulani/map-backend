from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache.backends.locmem import LocMemCache
from django.test import override_settings
from django.utils.timezone import now
from rest_framework.exceptions import Throttled

from apps.core.models import BackgroundJobDispatch, TenantDailyPaidUsage
from apps.core.dispatch import SafeRetryableDispatchError
from apps.products.models import Product, ProductParseJob
from apps.tenants.services import TenantService
from apps.web_research.models import WebResearchRun
from apps.web_research.services import (
    WebResearchService, WebResearchTerminalSearchFailure,
    WebSearchOutcomeUncertain,
)
from apps.web_research.tasks import run_web_research, schedule_web_research_fallback


def test_web_research_tasks_use_a_queue_served_in_production():
    assert run_web_research.queue == 'part_parsing'
    assert schedule_web_research_fallback.queue == 'part_parsing'


def test_web_research_fails_closed_without_redis_coordination_cache(monkeypatch):
    monkeypatch.setattr(
        'apps.web_research.tasks.cache',
        LocMemCache('web-research-no-lock', {}),
    )

    with pytest.raises(RuntimeError, match='must be RedisCache'):
        run_web_research.run(1)


@pytest.mark.django_db
def test_web_research_lock_contention_stays_retryable_for_durable_dispatch():
    tenant, _ = TenantService.create_tenant(
        'web-lock-contention', 'web-lock-contention',
        'web-lock-contention@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='LOCK-CONTENTION',
        name='Lock contention product',
        price=Decimal('0'),
    )
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    lock = MagicMock()
    lock.acquire.return_value = False

    class FakeRedisCache:
        def lock(self, *_args, **_kwargs):
            return lock

    with patch('apps.web_research.tasks.RedisCache', FakeRedisCache), patch(
        'apps.web_research.tasks.cache', FakeRedisCache(),
    ), pytest.raises(
        SafeRetryableDispatchError,
        match='already owned',
    ):
        run_web_research.run(run.pk)

    lock.release.assert_not_called()
    run.refresh_from_db()
    assert run.status == WebResearchRun.Status.QUEUED


@pytest.mark.django_db
def test_terminal_web_research_replay_is_noop_before_coordination_lock():
    tenant, _ = TenantService.create_tenant(
        'web-terminal-noop', 'web-terminal-noop',
        'web-terminal-noop@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='TERMINAL-NOOP',
        name='Terminal no-op product',
        price=Decimal('0'),
    )
    run = WebResearchRun.objects.create(
        tenant=tenant,
        product=product,
        status=WebResearchRun.Status.NO_RESULTS,
    )

    with patch('apps.web_research.tasks.cache') as coordination:
        result = run_web_research.run(run.pk)

    assert result['status'] == WebResearchRun.Status.NO_RESULTS
    coordination.lock.assert_not_called()


@pytest.mark.django_db
def test_sparse_product_schedules_web_research_fallback():
    tenant, _ = TenantService.create_tenant(
        'web-fallback', 'web-fallback', 'web-fallback@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='OEM0099FONR',
        name='Фонарь правый внешний Kia Optima JF',
        category_1c='Автосвет',
        price=Decimal('0'),
    )

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        result = schedule_web_research_fallback.run(product.pk)

    run = WebResearchRun.objects.get(product=product)
    assert run.trigger == WebResearchRun.Trigger.PARSER_FALLBACK
    assert result['run_id'] == run.pk
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.web_research.tasks.run_web_research',
        args=[run.pk],
    ).count() == 1


@pytest.mark.django_db
def test_parser_fallback_origin_replay_consumes_one_budget_and_one_dispatch():
    tenant, _ = TenantService.create_tenant(
        'web-fallback-origin', 'web-fallback-origin',
        'web-fallback-origin@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='ORIGIN-1',
        name='Sparse origin product',
        price=Decimal('0'),
    )

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        first = schedule_web_research_fallback.run(
            product.pk, False, 'product-parse-intent:stable',
        )
        second = schedule_web_research_fallback.run(
            product.pk, False, 'product-parse-intent:stable',
        )

    assert first['run_id'] == second['run_id']
    assert WebResearchRun.objects.filter(product=product).count() == 1
    assert TenantDailyPaidUsage.objects.get(
        tenant=tenant, scope='web-research-starts',
    ).units == 1
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.web_research.tasks.run_web_research',
    ).count() == 1


@pytest.mark.django_db
def test_parser_fallback_waits_for_every_exact_origin_sibling_without_age_cutoff():
    tenant, _ = TenantService.create_tenant(
        'web-fallback-all-siblings', 'web-fallback-all-siblings',
        'web-fallback-all-siblings@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='ALL-SIBLINGS-1',
        name='Sparse sibling product',
        price=Decimal('0'),
    )
    origin = 'product-parse-intent:all-sources'
    jobs = {}
    for source_id, status in (
        ('tachka', ProductParseJob.Status.NOT_FOUND),
        ('rossko', ProductParseJob.Status.FAILED),
        ('euroauto', ProductParseJob.Status.RUNNING),
    ):
        jobs[source_id] = ProductParseJob.objects.create(
            tenant=tenant,
            product=product,
            fallback_origin_key=origin,
            brand='Brand',
            article=product.article,
            normalized_article='ALLSIBLINGS1',
            source_id=source_id,
            status=status,
        )
    ProductParseJob.objects.filter(pk=jobs['euroauto'].pk).update(
        created_at=now() - timedelta(hours=2),
        updated_at=now() - timedelta(hours=2),
    )

    with pytest.raises(SafeRetryableDispatchError, match='not terminal'):
        schedule_web_research_fallback.run(product.pk, False, origin)

    assert not WebResearchRun.objects.filter(product=product).exists()
    assert not TenantDailyPaidUsage.objects.filter(tenant=tenant).exists()
    assert not BackgroundJobDispatch.objects.filter(
        task_name='apps.web_research.tasks.run_web_research',
    ).exists()

    jobs['euroauto'].status = ProductParseJob.Status.NOT_FOUND
    jobs['euroauto'].finished_at = now()
    jobs['euroauto'].save(update_fields=['status', 'finished_at', 'updated_at'])
    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        first = schedule_web_research_fallback.run(product.pk, False, origin)
        second = schedule_web_research_fallback.run(product.pk, False, origin)

    assert first['run_id'] == second['run_id']
    assert WebResearchRun.objects.filter(product=product).count() == 1
    assert TenantDailyPaidUsage.objects.get(
        tenant=tenant, scope='web-research-starts',
    ).units == 1
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.web_research.tasks.run_web_research',
    ).count() == 1


@pytest.mark.django_db
def test_parser_fallback_replay_recovers_missing_canonical_dispatch():
    tenant, _ = TenantService.create_tenant(
        'web-fallback-dispatch-recovery', 'web-fallback-dispatch-recovery',
        'web-fallback-dispatch-recovery@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='RECOVER-DISPATCH',
        name='Sparse recovery product',
        price=Decimal('0'),
    )
    run, created = WebResearchService.create_run(
        product,
        trigger=WebResearchRun.Trigger.PARSER_FALLBACK,
        origin_key='parse:lost-before-dispatch',
    )
    assert created is True
    assert not BackgroundJobDispatch.objects.filter(
        task_name='apps.web_research.tasks.run_web_research',
    ).exists()

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        result = schedule_web_research_fallback.run(
            product.pk, False, 'parse:lost-before-dispatch',
        )

    assert result['run_id'] == run.pk
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.web_research.tasks.run_web_research', args=[run.pk],
    ).count() == 1
    assert TenantDailyPaidUsage.objects.get(
        tenant=tenant, scope='web-research-starts',
    ).units == 1


@pytest.mark.django_db
@override_settings(WEB_RESEARCH_TENANT_DAILY_STARTS=1)
def test_parser_fallback_budget_exhaustion_creates_no_second_run_or_dispatch():
    tenant, _ = TenantService.create_tenant(
        'web-fallback-budget', 'web-fallback-budget',
        'web-fallback-budget@test.com', 'pass12345',
    )
    first_product = Product.objects.create(
        tenant=tenant, article='BUDGET-1', name='First sparse', price=Decimal('0'),
    )
    second_product = Product.objects.create(
        tenant=tenant, article='BUDGET-2', name='Second sparse', price=Decimal('0'),
    )

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        schedule_web_research_fallback.run(
            first_product.pk, False, 'parse:first',
        )
        with pytest.raises(Throttled):
            schedule_web_research_fallback.run(
                second_product.pk, False, 'parse:second',
            )

    assert WebResearchRun.objects.filter(tenant=tenant).count() == 1
    assert not WebResearchRun.objects.filter(product=second_product).exists()
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.web_research.tasks.run_web_research',
    ).count() == 1


@pytest.mark.django_db
def test_terminal_origin_upgrade_dispatches_ai_exactly_once_without_new_search():
    tenant, _ = TenantService.create_tenant(
        'web-fallback-upgrade', 'web-fallback-upgrade',
        'web-fallback-upgrade@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='ORIGIN-UPGRADE',
        name='Sparse upgrade product',
        price=Decimal('0'),
    )
    origin = 'product-parse-intent:upgrade'

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        first = schedule_web_research_fallback.run(product.pk, False, origin)
        run = WebResearchRun.objects.get(pk=first['run_id'])
        run.status = WebResearchRun.Status.NO_RESULTS
        run.save(update_fields=['status', 'updated_at'])
        second = schedule_web_research_fallback.run(product.pk, True, origin)
        third = schedule_web_research_fallback.run(product.pk, True, origin)

    run.refresh_from_db()
    assert second['run_id'] == run.pk == third['run_id']
    assert run.generate_after is True
    assert TenantDailyPaidUsage.objects.get(
        tenant=tenant, scope='web-research-starts',
    ).units == 1
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.web_research.tasks.run_web_research',
    ).count() == 1
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.ai_agent.tasks.generate_description_task',
        args=[product.pk],
    ).count() == 1


@pytest.mark.django_db
def test_failed_origin_upgrade_never_dispatches_ungrounded_ai():
    tenant, _ = TenantService.create_tenant(
        'web-fallback-failed-upgrade', 'web-fallback-failed-upgrade',
        'web-fallback-failed-upgrade@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='ORIGIN-FAILED',
        name='Failed origin product',
        price=Decimal('0'),
    )
    origin = 'product-parse-intent:failed'
    run = WebResearchRun.objects.create(
        tenant=tenant,
        product=product,
        origin_key=origin,
        status=WebResearchRun.Status.FAILED,
    )

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        result = schedule_web_research_fallback.run(product.pk, True, origin)

    run.refresh_from_db()
    assert result['run_id'] == run.pk
    assert run.generate_after is True
    assert not BackgroundJobDispatch.objects.filter(
        task_name='apps.ai_agent.tasks.generate_description_task',
    ).exists()


@pytest.mark.django_db
def test_uncertain_paid_search_is_never_retried_by_legacy_celery_path():
    tenant, _ = TenantService.create_tenant(
        'web-uncertain-task', 'web-uncertain-task',
        'web-uncertain-task@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='UNCERTAIN',
        name='Uncertain provider task',
        price=Decimal('0'),
    )
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    lock = MagicMock()
    lock.acquire.return_value = True

    class FakeRedisCache:
        def lock(self, *_args, **_kwargs):
            return lock

    with patch('apps.web_research.tasks.RedisCache', FakeRedisCache), patch(
        'apps.web_research.tasks.cache', FakeRedisCache(),
    ), patch(
        'apps.web_research.tasks.WebResearchService.execute',
        side_effect=WebSearchOutcomeUncertain('unknown'),
    ), patch.object(
        run_web_research, 'retry',
    ) as retry, pytest.raises(WebSearchOutcomeUncertain):
        run_web_research.run(run.pk)

    retry.assert_not_called()
    lock.release.assert_called_once_with()
    assert not BackgroundJobDispatch.objects.filter(
        task_name='apps.ai_agent.tasks.generate_description_task',
    ).exists()


@pytest.mark.django_db
def test_retryable_web_task_failure_is_delegated_to_durable_dispatch():
    tenant, _ = TenantService.create_tenant(
        'web-durable-retry', 'web-durable-retry',
        'web-durable-retry@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='DURABLE-RETRY',
        name='Durable retry product',
        price=Decimal('0'),
    )
    run = WebResearchRun.objects.create(
        tenant=tenant,
        product=product,
        status=WebResearchRun.Status.RUNNING,
    )
    lock = MagicMock()
    lock.acquire.return_value = True

    class FakeRedisCache:
        def lock(self, *_args, **_kwargs):
            return lock

    with patch('apps.web_research.tasks.RedisCache', FakeRedisCache), patch(
        'apps.web_research.tasks.cache', FakeRedisCache(),
    ), patch(
        'apps.web_research.tasks.WebResearchService.execute',
        side_effect=RuntimeError('local apply failed'),
    ), patch.object(run_web_research, 'retry') as celery_retry, pytest.raises(
        SafeRetryableDispatchError,
        match='local apply failed',
    ):
        run_web_research.run(run.pk)

    celery_retry.assert_not_called()
    run.refresh_from_db()
    assert run.status == WebResearchRun.Status.QUEUED
    assert run.finished_at is None


@pytest.mark.django_db
def test_authoritative_terminal_search_failure_is_not_retried_or_generated():
    tenant, _ = TenantService.create_tenant(
        'web-terminal-search-task', 'web-terminal-search-task',
        'web-terminal-search-task@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='TERMINAL-SEARCH',
        name='Terminal provider plan',
        price=Decimal('0'),
    )
    run = WebResearchRun.objects.create(
        tenant=tenant,
        product=product,
        status=WebResearchRun.Status.FAILED,
    )
    lock = MagicMock()
    lock.acquire.return_value = True

    class FakeRedisCache:
        def lock(self, *_args, **_kwargs):
            return lock

    with patch('apps.web_research.tasks.RedisCache', FakeRedisCache), patch(
        'apps.web_research.tasks.cache', FakeRedisCache(),
    ), patch(
        'apps.web_research.tasks.WebResearchService.execute',
        side_effect=WebResearchTerminalSearchFailure('authoritative reject'),
    ), patch.object(run_web_research, 'retry') as retry:
        result = run_web_research.run(run.pk)

    assert result['status'] == WebResearchRun.Status.FAILED
    retry.assert_not_called()
    assert not BackgroundJobDispatch.objects.filter(
        task_name='apps.ai_agent.tasks.generate_description_task',
    ).exists()
