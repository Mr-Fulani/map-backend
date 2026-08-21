from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.billing.models import BillingOutboxEvent
from apps.core.retention import purge_retained_data
from apps.tenants.services import TenantService


@pytest.mark.django_db
def test_retention_purges_resolved_notification_delivery_only(settings):
    from apps.notifications.models import NotificationDelivery

    settings.NOTIFICATION_DELIVERY_RETENTION_DAYS = 30
    tenant, _ = TenantService.create_tenant(
        'Notification retention',
        'notification-retention',
        'notification-retention@example.com',
        'pass12345',
    )
    sent = NotificationDelivery.objects.create(
        tenant=tenant,
        event_key='sent',
        channel=NotificationDelivery.Channel.EMAIL,
        payload_fingerprint='a' * 64,
        status=NotificationDelivery.Status.SENT,
    )
    uncertain = NotificationDelivery.objects.create(
        tenant=tenant,
        event_key='uncertain',
        channel=NotificationDelivery.Channel.TELEGRAM,
        payload_fingerprint='b' * 64,
        status=NotificationDelivery.Status.OUTCOME_UNCERTAIN,
    )
    old = timezone.now() - timedelta(days=31)
    NotificationDelivery.objects.filter(pk__in=[sent.pk, uncertain.pk]).update(
        updated_at=old,
    )

    result = purge_retained_data()

    assert result['notification_deliveries'] == 1
    assert not NotificationDelivery.objects.filter(pk=sent.pk).exists()
    assert NotificationDelivery.objects.filter(pk=uncertain.pk).exists()


@pytest.mark.django_db
def test_retention_deletes_only_expired_dispatched_billing_outbox(settings):
    settings.BILLING_AUDIT_RETENTION_DAYS = 30
    tenant, _ = TenantService.create_tenant(
        'Retention Corp',
        'retention-outbox',
        'retention@example.com',
        'pass12345',
    )
    old = timezone.now() - timedelta(days=31)
    fresh = timezone.now() - timedelta(days=29)

    expired = BillingOutboxEvent.objects.create(
        tenant=tenant,
        event_type=BillingOutboxEvent.EVENT_NOTIFICATION,
        idempotency_key='expired:v1',
        payload={'schema': 1},
        status=BillingOutboxEvent.STATUS_DISPATCHED,
        dispatched_at=old,
    )
    retained = BillingOutboxEvent.objects.create(
        tenant=tenant,
        event_type=BillingOutboxEvent.EVENT_NOTIFICATION,
        idempotency_key='fresh:v1',
        payload={'schema': 1},
        status=BillingOutboxEvent.STATUS_DISPATCHED,
        dispatched_at=fresh,
    )
    pending = BillingOutboxEvent.objects.create(
        tenant=tenant,
        event_type=BillingOutboxEvent.EVENT_NOTIFICATION,
        idempotency_key='pending:v1',
        payload={'schema': 1},
        status=BillingOutboxEvent.STATUS_PENDING,
        dispatched_at=old,
    )

    result = purge_retained_data()

    assert result['billing_outbox_events'] == 1
    assert not BillingOutboxEvent.objects.filter(pk=expired.pk).exists()
    assert BillingOutboxEvent.objects.filter(pk=retained.pk).exists()
    assert BillingOutboxEvent.objects.filter(pk=pending.pk).exists()


@pytest.mark.django_db
def test_retention_purges_only_expired_terminal_operational_artifacts(settings):
    from apps.core.models import BackgroundJobDispatch
    from apps.image_search.models import ImageSearchCache, ImageSearchLog, ImageSearchTask
    from apps.media_processing.models import MediaProcessingJob
    from apps.products.models import Product, ProductBulkActionJob, ProductImage, ProductParseJob

    settings.PRODUCT_PARSE_RAW_HTML_RETENTION_DAYS = 7
    settings.PRODUCT_PARSE_JOB_RETENTION_DAYS = 30
    settings.IMAGE_SEARCH_LOG_RETENTION_DAYS = 30
    settings.IMAGE_SEARCH_TASK_RETENTION_DAYS = 30
    settings.PRODUCT_BULK_ACTION_JOB_RETENTION_DAYS = 30
    settings.MEDIA_PROCESSING_JOB_RETENTION_DAYS = 30
    settings.BACKGROUND_JOB_RETENTION_DAYS = 30
    settings.RETENTION_PURGE_BATCH_SIZE = 1000
    tenant, _ = TenantService.create_tenant(
        'Operational Retention',
        'operational-retention',
        'operational-retention@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='RET-1',
        name='Retention product',
        price='1.00',
    )
    image = ProductImage.objects.create(
        product=product,
        s3_key='dev/products/retention/original.jpg',
        sha256='retention-source',
    )
    parse_for_raw_cleanup = ProductParseJob.objects.create(
        tenant=tenant,
        product=product,
        brand='Brand',
        article='RET-1',
        normalized_article='RET1',
        status=ProductParseJob.Status.SUCCESS,
        raw_html='<html>large</html>',
    )
    expired_parse = ProductParseJob.objects.create(
        tenant=tenant,
        product=product,
        brand='Brand',
        article='RET-2',
        normalized_article='RET2',
        status=ProductParseJob.Status.FAILED,
        raw_html='<html>expired</html>',
    )
    active_parse = ProductParseJob.objects.create(
        tenant=tenant,
        product=product,
        brand='Brand',
        article='RET-3',
        normalized_article='RET3',
        status=ProductParseJob.Status.RUNNING,
        raw_html='<html>active</html>',
    )
    expired_log = ImageSearchLog.objects.create(
        tenant=tenant,
        product=product,
        source_id='test',
        query='expired',
    )
    fresh_log = ImageSearchLog.objects.create(
        tenant=tenant,
        product=product,
        source_id='test',
        query='fresh',
    )
    expired_task = ImageSearchTask.objects.create(
        tenant=tenant,
        product=product,
        task_id='expired-image-task',
    )
    fresh_task = ImageSearchTask.objects.create(
        tenant=tenant,
        product=product,
        task_id='fresh-image-task',
    )
    expired_cache = ImageSearchCache.objects.create(
        cache_key='retention:expired',
        results=[],
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    fresh_cache = ImageSearchCache.objects.create(
        cache_key='retention:fresh',
        results=[],
        expires_at=timezone.now() + timedelta(days=1),
    )
    terminal_bulk = ProductBulkActionJob.objects.create(
        tenant=tenant,
        action=ProductBulkActionJob.Action.FIND_IMAGES,
        product_ids=[product.pk],
        status=ProductBulkActionJob.Status.SUCCESS,
    )
    active_bulk = ProductBulkActionJob.objects.create(
        tenant=tenant,
        action=ProductBulkActionJob.Action.FIND_IMAGES,
        product_ids=[product.pk],
        status=ProductBulkActionJob.Status.COOLING_DOWN,
    )
    terminal_media = MediaProcessingJob.objects.create(
        tenant=tenant,
        product_image=image,
        operations=['resize'],
        status=MediaProcessingJob.Status.FAILED,
    )
    active_media = MediaProcessingJob.objects.create(
        tenant=tenant,
        product_image=image,
        operations=['resize'],
        status=MediaProcessingJob.Status.PROCESSING,
    )
    terminal_dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.tests.terminal',
        queue='default',
        status=BackgroundJobDispatch.Status.SUCCEEDED,
        finished_at=timezone.now(),
    )
    active_dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.tests.active',
        queue='default',
        status=BackgroundJobDispatch.Status.PUBLISHED,
        finished_at=timezone.now(),
    )
    now = timezone.now()
    ten_days_old = now - timedelta(days=10)
    expired = now - timedelta(days=31)
    ProductParseJob.objects.filter(pk=parse_for_raw_cleanup.pk).update(
        created_at=ten_days_old,
        updated_at=ten_days_old,
    )
    ProductParseJob.objects.filter(pk__in=[expired_parse.pk, active_parse.pk]).update(
        created_at=expired,
        updated_at=expired,
    )
    ImageSearchLog.objects.filter(pk=expired_log.pk).update(created_at=expired, updated_at=expired)
    ImageSearchTask.objects.filter(pk=expired_task.pk).update(created_at=expired, updated_at=expired)
    ProductBulkActionJob.objects.filter(pk__in=[terminal_bulk.pk, active_bulk.pk]).update(
        created_at=expired,
        updated_at=expired,
    )
    MediaProcessingJob.objects.filter(pk__in=[terminal_media.pk, active_media.pk]).update(
        created_at=expired,
        updated_at=expired,
    )
    BackgroundJobDispatch.objects.filter(
        pk__in=[terminal_dispatch.pk, active_dispatch.pk],
    ).update(finished_at=expired, updated_at=expired)

    result = purge_retained_data()

    parse_for_raw_cleanup.refresh_from_db()
    active_parse.refresh_from_db()
    assert parse_for_raw_cleanup.raw_html == ''
    assert active_parse.raw_html == '<html>active</html>'
    assert not ProductParseJob.objects.filter(pk=expired_parse.pk).exists()
    assert not ImageSearchLog.objects.filter(pk=expired_log.pk).exists()
    assert ImageSearchLog.objects.filter(pk=fresh_log.pk).exists()
    assert not ImageSearchTask.objects.filter(pk=expired_task.pk).exists()
    assert ImageSearchTask.objects.filter(pk=fresh_task.pk).exists()
    assert not ImageSearchCache.objects.filter(pk=expired_cache.pk).exists()
    assert ImageSearchCache.objects.filter(pk=fresh_cache.pk).exists()
    assert not ProductBulkActionJob.objects.filter(pk=terminal_bulk.pk).exists()
    assert ProductBulkActionJob.objects.filter(pk=active_bulk.pk).exists()
    assert not MediaProcessingJob.objects.filter(pk=terminal_media.pk).exists()
    assert MediaProcessingJob.objects.filter(pk=active_media.pk).exists()
    assert not BackgroundJobDispatch.objects.filter(pk=terminal_dispatch.pk).exists()
    assert BackgroundJobDispatch.objects.filter(pk=active_dispatch.pk).exists()
    assert result['product_parse_raw_html'] == 2
    assert result['product_parse_jobs'] == 1
    assert result['image_search_logs'] == 1
    assert result['image_search_tasks'] == 1
    assert result['expired_image_search_cache'] == 1
    assert result['product_bulk_action_jobs'] == 1
    assert result['media_processing_jobs'] == 1
    assert result['background_job_dispatches'] == 1


@pytest.mark.django_db
def test_retention_purge_is_bounded_per_artifact_type(settings):
    from apps.image_search.models import ImageSearchCache

    settings.RETENTION_PURGE_BATCH_SIZE = 1
    expired_at = timezone.now() - timedelta(days=1)
    for suffix in ('one', 'two'):
        ImageSearchCache.objects.create(
            cache_key=f'bounded:{suffix}',
            results=[],
            expires_at=expired_at,
        )

    result = purge_retained_data()

    assert result['expired_image_search_cache'] == 1
    assert ImageSearchCache.objects.filter(cache_key__startswith='bounded:').count() == 1


@pytest.mark.django_db
def test_retention_purges_only_safe_paid_intents_and_resolved_search_evidence(settings):
    import uuid

    from apps.core.models import (
        BackgroundJobDispatch, PaidIngressIntent, TenantDailyPaidUsage,
    )
    from apps.web_research.models import WebSearchAttempt, WebSearchWorkflow

    settings.BACKGROUND_JOB_RETENTION_DAYS = 30
    settings.WEB_SEARCH_ATTEMPT_RETENTION_DAYS = 30
    settings.RETENTION_PURGE_BATCH_SIZE = 100
    retention_now = timezone.now()
    tenant, _ = TenantService.create_tenant(
        'Paid evidence retention',
        'paid-evidence-retention',
        'paid-evidence-retention@example.com',
        'pass12345',
    )
    terminal = BackgroundJobDispatch.objects.create(
        task_name='apps.ai_agent.tasks.generate_description_task',
        queue='ai_generate',
        status=BackgroundJobDispatch.Status.SUCCEEDED,
        finished_at=timezone.now(),
    )
    active = BackgroundJobDispatch.objects.create(
        task_name='apps.ai_agent.tasks.generate_description_task',
        queue='ai_generate',
        status=BackgroundJobDispatch.Status.RUNNING,
    )
    old_safe_intent = PaidIngressIntent.objects.create(
        tenant=tenant,
        operation='listing-regenerate',
        idempotency_key=uuid.uuid4(),
        request_fingerprint='a' * 64,
        raw_payload_fingerprint='c' * 64,
        request_payload={},
        resource_type='marketplaces.listing',
        resource_id='1',
        dispatch=terminal,
    )
    old_active_intent = PaidIngressIntent.objects.create(
        tenant=tenant,
        operation='listing-regenerate',
        idempotency_key=uuid.uuid4(),
        request_fingerprint='b' * 64,
        raw_payload_fingerprint='d' * 64,
        request_payload={},
        resource_type='marketplaces.listing',
        resource_id='2',
        dispatch=active,
    )
    resolved_workflow = WebSearchWorkflow.objects.create(
        tenant=tenant,
        operation='euroauto',
        domain_reference='product:resolved',
        workflow_key='retention:resolved',
        input_fingerprint='1' * 64,
        input_snapshot={'version': 1},
        status=WebSearchWorkflow.Status.RECONCILED,
        reconciled_at=timezone.now(),
    )
    pending_workflow = WebSearchWorkflow.objects.create(
        tenant=tenant,
        operation='euroauto',
        domain_reference='product:pending',
        workflow_key='retention:pending',
        input_fingerprint='2' * 64,
        input_snapshot={'version': 1},
        status=WebSearchWorkflow.Status.UNCERTAIN,
    )
    apply_pending_workflow = WebSearchWorkflow.objects.create(
        tenant=tenant,
        operation='image_search',
        domain_reference='product:checkpoint-pending',
        workflow_key='retention:checkpoint-pending',
        input_fingerprint='5' * 64,
        input_snapshot={'version': 1},
        status=WebSearchWorkflow.Status.APPLY_PENDING,
    )
    old_resolved = WebSearchAttempt.objects.create(
        tenant=tenant,
        workflow=resolved_workflow,
        provider_id='brave',
        operation='euroauto',
        domain_reference='product:resolved',
        call_key='brave:text:resolved',
        request_fingerprint='3' * 64,
        query='resolved',
        status=WebSearchAttempt.Status.OUTCOME_UNCERTAIN,
        reconciliation_state=WebSearchAttempt.ReconciliationState.RESOLVED,
        reconciliation_action='not_accepted',
        reconciled_at=timezone.now(),
        apply_state=WebSearchAttempt.ApplyState.APPLIED,
    )
    old_pending = WebSearchAttempt.objects.create(
        tenant=tenant,
        workflow=pending_workflow,
        provider_id='tavily',
        operation='euroauto',
        domain_reference='product:pending',
        call_key='tavily:text:pending',
        request_fingerprint='4' * 64,
        query='pending',
        status=WebSearchAttempt.Status.STARTED,
        reconciliation_state=WebSearchAttempt.ReconciliationState.PENDING,
        apply_state=WebSearchAttempt.ApplyState.PENDING,
    )
    old_apply_pending = WebSearchAttempt.objects.create(
        tenant=tenant,
        workflow=apply_pending_workflow,
        provider_id='brave',
        operation='image_search',
        domain_reference='product:checkpoint-pending',
        call_kind='image',
        call_key='brave:image:pending',
        request_fingerprint='6' * 64,
        query='checkpoint pending',
        status=WebSearchAttempt.Status.SUCCESS,
        reconciliation_state=WebSearchAttempt.ReconciliationState.NOT_REQUIRED,
        apply_state=WebSearchAttempt.ApplyState.PENDING,
    )
    old_usage = TenantDailyPaidUsage.objects.create(
        tenant=tenant,
        scope='web-research-starts',
        usage_date=(retention_now - timedelta(days=31)).date(),
        units=3,
    )
    fresh_usage = TenantDailyPaidUsage.objects.create(
        tenant=tenant,
        scope='image-search-jobs',
        usage_date=(retention_now - timedelta(days=29)).date(),
        units=2,
    )
    expired = retention_now - timedelta(days=31)
    PaidIngressIntent.objects.filter(
        pk__in=[old_safe_intent.pk, old_active_intent.pk],
    ).update(created_at=expired, updated_at=expired)
    BackgroundJobDispatch.objects.filter(pk=terminal.pk).update(
        finished_at=expired,
        updated_at=expired,
    )
    WebSearchAttempt.objects.filter(
        pk__in=[old_resolved.pk, old_pending.pk, old_apply_pending.pk],
    ).update(created_at=expired, updated_at=expired)
    WebSearchAttempt.objects.filter(pk=old_resolved.pk).update(reconciled_at=expired)
    WebSearchWorkflow.objects.filter(
        pk__in=[
            resolved_workflow.pk,
            pending_workflow.pk,
            apply_pending_workflow.pk,
        ],
    ).update(created_at=expired, updated_at=expired)
    WebSearchWorkflow.objects.filter(pk=resolved_workflow.pk).update(
        reconciled_at=expired,
    )

    with patch('apps.core.retention.timezone.now', return_value=retention_now):
        result = purge_retained_data()

    assert result['paid_ingress_intents'] == 1
    assert result['tenant_daily_paid_usage'] == 1
    assert result['web_search_attempts'] == 1
    assert not PaidIngressIntent.objects.filter(pk=old_safe_intent.pk).exists()
    assert PaidIngressIntent.objects.filter(pk=old_active_intent.pk).exists()
    assert not WebSearchAttempt.objects.filter(pk=old_resolved.pk).exists()
    assert WebSearchAttempt.objects.filter(pk=old_pending.pk).exists()
    assert WebSearchAttempt.objects.filter(pk=old_apply_pending.pk).exists()
    # Workflow selection happened before its child was purged; the next
    # bounded cycle removes the now-empty terminal owner, never the active one.
    with patch('apps.core.retention.timezone.now', return_value=retention_now):
        second = purge_retained_data()
    assert second['web_search_workflows'] == 1
    assert not WebSearchWorkflow.objects.filter(pk=resolved_workflow.pk).exists()
    assert WebSearchWorkflow.objects.filter(pk=pending_workflow.pk).exists()
    assert WebSearchWorkflow.objects.filter(pk=apply_pending_workflow.pk).exists()
    assert not TenantDailyPaidUsage.objects.filter(pk=old_usage.pk).exists()
    assert TenantDailyPaidUsage.objects.filter(pk=fresh_usage.pk).exists()


@pytest.mark.django_db
def test_retention_keeps_durable_owners_of_active_web_search_workflows(settings):
    import uuid

    from apps.image_search.models import ImageSearchTask
    from apps.products.models import Product, ProductParseIntent, ProductParseJob
    from apps.web_research.models import WebSearchWorkflow

    settings.IMAGE_SEARCH_TASK_RETENTION_DAYS = 30
    settings.PRODUCT_PARSE_JOB_RETENTION_DAYS = 30
    settings.RETENTION_PURGE_BATCH_SIZE = 100
    tenant, _ = TenantService.create_tenant(
        'Workflow owner retention',
        'workflow-owner-retention',
        'workflow-owner-retention@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='WORKFLOW-OWNER-RETENTION',
        name='Workflow owner retention',
        price='1.00',
    )
    parse_intent = ProductParseIntent.objects.create(
        tenant=tenant,
        product=product,
        idempotency_key=uuid.uuid4(),
        request_fingerprint='a' * 64,
        request_payload={'product_id': product.pk},
    )
    parse_job = ProductParseJob.objects.create(
        tenant=tenant,
        product=product,
        ingress_intent=parse_intent,
        brand='',
        article=product.article,
        normalized_article='WORKFLOWOWNERRETENTION',
        source_id='euroauto',
        status=ProductParseJob.Status.FAILED,
    )
    parse_intent.primary_job = parse_job
    parse_intent.job_ids = [parse_job.pk]
    parse_intent.save(update_fields=['primary_job', 'job_ids', 'updated_at'])
    image_task = ImageSearchTask.objects.create(
        tenant=tenant,
        product=product,
        task_id='workflow-owner-retention-image',
        status=ImageSearchTask.Status.SUCCEEDED,
        result={'count': 0},
        finished_at=timezone.now(),
    )
    WebSearchWorkflow.objects.create(
        tenant=tenant,
        product=product,
        operation='euroauto',
        domain_reference=f'product:{tenant.pk}:{product.pk}',
        workflow_key=f'product-parse-job:{parse_job.pk}',
        input_fingerprint='b' * 64,
        input_snapshot={'version': 1},
        status=WebSearchWorkflow.Status.APPLY_PENDING,
    )
    WebSearchWorkflow.objects.create(
        tenant=tenant,
        product=product,
        operation='image_search',
        domain_reference=f'product:{tenant.pk}:{product.pk}',
        workflow_key=f'image-search-task:{image_task.pk}',
        input_fingerprint='c' * 64,
        input_snapshot={'version': 1},
        status=WebSearchWorkflow.Status.APPLY_PENDING,
    )
    expired = timezone.now() - timedelta(days=31)
    ProductParseIntent.objects.filter(pk=parse_intent.pk).update(
        created_at=expired,
        updated_at=expired,
    )
    ProductParseJob.objects.filter(pk=parse_job.pk).update(
        created_at=expired,
        updated_at=expired,
    )
    ImageSearchTask.objects.filter(pk=image_task.pk).update(
        created_at=expired,
        updated_at=expired,
        finished_at=expired,
    )

    result = purge_retained_data()

    assert result['product_parse_intents'] == 0
    assert result['product_parse_jobs'] == 0
    assert result['image_search_tasks'] == 0
    assert ProductParseIntent.objects.filter(pk=parse_intent.pk).exists()
    assert ProductParseJob.objects.filter(pk=parse_job.pk).exists()
    assert ImageSearchTask.objects.filter(pk=image_task.pk).exists()


@pytest.mark.django_db
def test_retention_keeps_product_for_applied_search_with_local_phase_pending(
    settings,
):
    from apps.products.models import Product
    from apps.web_research.models import (
        WebResearchRun,
        WebSearchAttempt,
        WebSearchWorkflow,
    )

    settings.SOFT_DELETE_RETENTION_DAYS = 30
    settings.RETENTION_PURGE_BATCH_SIZE = 100
    tenant, _ = TenantService.create_tenant(
        'Applied search local resume retention',
        'applied-search-local-resume-retention',
        'applied-search-local-resume-retention@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='APPLIED-LOCAL-RESUME',
        name='Applied local resume owner',
        price='1.00',
    )
    run = WebResearchRun.objects.create(
        tenant=tenant,
        product=product,
        status=WebResearchRun.Status.QUEUED,
    )
    workflow = WebSearchWorkflow.objects.create(
        tenant=tenant,
        product=None,
        run=run,
        operation='web_research',
        domain_reference=f'product:{product.pk}:purpose:enrichment',
        workflow_key=f'web-research-run:{run.pk}',
        input_fingerprint='d' * 64,
        input_snapshot={'version': 1},
        status=WebSearchWorkflow.Status.APPLIED,
        applied_at=timezone.now(),
    )
    WebSearchAttempt.objects.create(
        tenant=tenant,
        workflow=workflow,
        run=run,
        provider_id='brave',
        operation='web_research',
        call_kind='text',
        domain_reference=workflow.domain_reference,
        call_key='brave:text:slot:0',
        request_fingerprint='e' * 64,
        query='durable applied checkpoint',
        status=WebSearchAttempt.Status.SUCCESS,
        checkpoint_enc=b'encrypted-checkpoint-placeholder',
        reconciliation_state=(
            WebSearchAttempt.ReconciliationState.NOT_REQUIRED
        ),
        apply_state=WebSearchAttempt.ApplyState.APPLIED,
    )
    expired = timezone.now() - timedelta(days=31)
    WebSearchAttempt.objects.filter(workflow=workflow).update(
        created_at=expired,
        updated_at=expired,
    )
    WebSearchWorkflow.objects.filter(pk=workflow.pk).update(
        created_at=expired,
        updated_at=expired,
        applied_at=expired,
    )
    Product.all_objects.filter(pk=product.pk).update(
        deleted_at=expired,
        updated_at=expired,
    )

    first = purge_retained_data()
    second = purge_retained_data()

    assert first['products'] == 0
    assert first['web_search_attempts'] == 0
    assert first['web_search_workflows'] == 0
    assert second['products'] == 0
    assert second['web_search_attempts'] == 0
    assert second['web_search_workflows'] == 0
    assert Product.all_objects.filter(pk=product.pk).exists()
    assert WebSearchWorkflow.objects.filter(pk=workflow.pk).exists()
    assert WebSearchAttempt.objects.filter(workflow=workflow).exists()


@pytest.mark.django_db
def test_retention_keeps_parse_intent_when_non_primary_job_owns_active_workflow(
    settings,
):
    import uuid

    from apps.products.models import Product, ProductParseIntent, ProductParseJob
    from apps.web_research.models import WebSearchWorkflow

    settings.PRODUCT_PARSE_JOB_RETENTION_DAYS = 30
    settings.RETENTION_PURGE_BATCH_SIZE = 100
    tenant, _ = TenantService.create_tenant(
        'Non-primary workflow owner',
        'non-primary-workflow-owner',
        'non-primary-workflow-owner@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='NON-PRIMARY-WORKFLOW',
        name='Non-primary workflow owner',
        price='1.00',
    )
    intent = ProductParseIntent.objects.create(
        tenant=tenant,
        product=product,
        idempotency_key=uuid.uuid4(),
        request_fingerprint='d' * 64,
        request_payload={'product_id': product.pk},
    )
    primary = ProductParseJob.objects.create(
        tenant=tenant,
        product=product,
        ingress_intent=intent,
        brand='',
        article=product.article,
        normalized_article='NONPRIMARYWORKFLOW',
        source_id='tachka',
        status=ProductParseJob.Status.FAILED,
    )
    paid_sibling = ProductParseJob.objects.create(
        tenant=tenant,
        product=product,
        ingress_intent=intent,
        brand='',
        article=product.article,
        normalized_article='NONPRIMARYWORKFLOW',
        source_id='euroauto',
        status=ProductParseJob.Status.FAILED,
    )
    intent.primary_job = primary
    intent.job_ids = [primary.pk, paid_sibling.pk]
    intent.save(update_fields=['primary_job', 'job_ids', 'updated_at'])
    WebSearchWorkflow.objects.create(
        tenant=tenant,
        product=product,
        operation='euroauto',
        domain_reference=f'product:{tenant.pk}:{product.pk}',
        workflow_key=f'product-parse-job:{paid_sibling.pk}',
        input_fingerprint='e' * 64,
        input_snapshot={'version': 1},
        status=WebSearchWorkflow.Status.APPLY_PENDING,
    )
    expired = timezone.now() - timedelta(days=31)
    ProductParseIntent.objects.filter(pk=intent.pk).update(
        created_at=expired,
        updated_at=expired,
    )
    ProductParseJob.objects.filter(pk__in=[primary.pk, paid_sibling.pk]).update(
        created_at=expired,
        updated_at=expired,
    )

    result = purge_retained_data()

    assert result['product_parse_intents'] == 0
    assert ProductParseIntent.objects.filter(pk=intent.pk).exists()
    assert ProductParseJob.objects.filter(pk=paid_sibling.pk).exists()


@pytest.mark.django_db
def test_retention_keeps_failed_parse_dispatch_until_paid_workflow_closes(
    settings,
):
    from apps.core.models import BackgroundJobDispatch
    from apps.products.models import Product, ProductParseJob
    from apps.web_research.models import WebSearchWorkflow

    settings.BACKGROUND_JOB_RETENTION_DAYS = 30
    settings.RETENTION_PURGE_BATCH_SIZE = 100
    tenant, _ = TenantService.create_tenant(
        'Euroauto dispatch retention',
        'euroauto-dispatch-retention',
        'euroauto-dispatch-retention@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='EUROAUTO-DISPATCH-RETENTION',
        name='Euroauto dispatch retention',
        price='1.00',
    )
    job = ProductParseJob.objects.create(
        tenant=tenant,
        product=product,
        brand='',
        article=product.article,
        normalized_article='EUROAUTODISPATCHRETENTION',
        source_id='euroauto',
        status=ProductParseJob.Status.FAILED,
    )
    dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.products.tasks.parse_single_part',
        queue='part_parsing',
        args=[job.pk],
        deduplication_key=f'product-parse-job:{job.pk}',
        status=BackgroundJobDispatch.Status.FAILED,
        finished_at=timezone.now(),
    )
    workflow = WebSearchWorkflow.objects.create(
        tenant=tenant,
        product=product,
        operation='euroauto',
        domain_reference=f'product:{tenant.pk}:{product.pk}',
        workflow_key=f'product-parse-job:{job.pk}',
        input_fingerprint='f' * 64,
        input_snapshot={'version': 1},
        status=WebSearchWorkflow.Status.APPLY_PENDING,
    )
    expired = timezone.now() - timedelta(days=31)
    BackgroundJobDispatch.objects.filter(pk=dispatch.pk).update(
        created_at=expired,
        updated_at=expired,
        finished_at=expired,
    )

    active = purge_retained_data()

    assert active['background_job_dispatches'] == 0
    assert BackgroundJobDispatch.objects.filter(pk=dispatch.pk).exists()

    WebSearchWorkflow.objects.filter(pk=workflow.pk).update(
        status=WebSearchWorkflow.Status.RECONCILED,
        reconciled_at=expired,
        updated_at=expired,
    )
    terminal = purge_retained_data()

    assert terminal['background_job_dispatches'] == 1
    assert not BackgroundJobDispatch.objects.filter(pk=dispatch.pk).exists()


@pytest.mark.django_db
def test_retention_expires_idempotency_intent_with_owned_artifacts(settings):
    import uuid

    from apps.core.models import BackgroundJobDispatch
    from apps.image_search.models import ImageSearchIntent, ImageSearchTask
    from apps.products.models import Product, ProductParseIntent, ProductParseJob

    settings.IMAGE_SEARCH_TASK_RETENTION_DAYS = 30
    settings.PRODUCT_PARSE_JOB_RETENTION_DAYS = 180
    settings.RETENTION_PURGE_BATCH_SIZE = 100
    tenant, _ = TenantService.create_tenant(
        'Intent retention',
        'intent-retention',
        'intent-retention@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='INTENT-1',
        name='Intent retention product',
        price='1.00',
    )
    image_intent = ImageSearchIntent.objects.create(
        tenant=tenant,
        operation=ImageSearchIntent.Operation.SINGLE,
        idempotency_key=uuid.uuid4(),
        request_fingerprint='a' * 64,
        request_payload={'product_id': product.pk},
    )
    terminal_dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.image_search.tasks.search_images_for_product',
        queue='image_search',
        status=BackgroundJobDispatch.Status.SUCCEEDED,
        finished_at=timezone.now(),
    )
    image_task = ImageSearchTask.objects.create(
        tenant=tenant,
        product=product,
        intent=image_intent,
        task_id='expired-owned-task',
        dispatch=terminal_dispatch,
    )
    active_image_intent = ImageSearchIntent.objects.create(
        tenant=tenant,
        operation=ImageSearchIntent.Operation.SINGLE,
        idempotency_key=uuid.uuid4(),
        request_fingerprint='d' * 64,
        request_payload={'product_id': product.pk},
    )
    active_dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.image_search.tasks.search_images_for_product',
        queue='image_search',
        status=BackgroundJobDispatch.Status.PUBLISHED,
    )
    active_image_task = ImageSearchTask.objects.create(
        tenant=tenant,
        product=product,
        intent=active_image_intent,
        task_id='active-owned-task',
        dispatch=active_dispatch,
    )
    parse_job = ProductParseJob.objects.create(
        tenant=tenant,
        product=product,
        brand='Brand',
        article='INTENT-1',
        normalized_article='INTENT1',
        status=ProductParseJob.Status.SUCCESS,
    )
    parse_intent = ProductParseIntent.objects.create(
        tenant=tenant,
        idempotency_key=uuid.uuid4(),
        request_fingerprint='b' * 64,
        request_payload={'product_id': product.pk},
        product=product,
        primary_job=parse_job,
        job_ids=[parse_job.pk],
    )
    parse_job.ingress_intent = parse_intent
    parse_job.save(update_fields=['ingress_intent', 'updated_at'])
    active_parse_job = ProductParseJob.objects.create(
        tenant=tenant,
        product=product,
        brand='Brand',
        article='INTENT-ACTIVE',
        normalized_article='INTENTACTIVE',
        status=ProductParseJob.Status.RUNNING,
    )
    active_parse_intent = ProductParseIntent.objects.create(
        tenant=tenant,
        idempotency_key=uuid.uuid4(),
        request_fingerprint='e' * 64,
        request_payload={'product_id': product.pk},
        product=product,
        primary_job=active_parse_job,
        job_ids=[active_parse_job.pk],
    )
    active_parse_job.ingress_intent = active_parse_intent
    active_parse_job.save(update_fields=['ingress_intent', 'updated_at'])
    retained_job = ProductParseJob.objects.create(
        tenant=tenant,
        product=product,
        brand='Brand',
        article='INTENT-FRESH',
        normalized_article='INTENTFRESH',
        status=ProductParseJob.Status.SUCCESS,
    )
    retained_intent = ProductParseIntent.objects.create(
        tenant=tenant,
        idempotency_key=uuid.uuid4(),
        request_fingerprint='c' * 64,
        request_payload={'product_id': product.pk},
        product=product,
        primary_job=retained_job,
        job_ids=[retained_job.pk],
    )
    now = timezone.now()
    image_expired = now - timedelta(days=31)
    parse_expired = now - timedelta(days=181)
    ImageSearchIntent.objects.filter(pk=image_intent.pk).update(created_at=image_expired)
    ImageSearchTask.objects.filter(pk=image_task.pk).update(created_at=image_expired)
    ImageSearchIntent.objects.filter(pk=active_image_intent.pk).update(
        created_at=image_expired,
    )
    ImageSearchTask.objects.filter(pk=active_image_task.pk).update(
        created_at=image_expired,
    )
    ProductParseIntent.objects.filter(
        pk__in=[parse_intent.pk, active_parse_intent.pk],
    ).update(created_at=parse_expired)
    ProductParseJob.objects.filter(
        pk__in=[parse_job.pk, retained_job.pk, active_parse_job.pk],
    ).update(created_at=parse_expired)

    result = purge_retained_data()

    assert result['image_search_intents'] == 1
    assert result['product_parse_intents'] == 1
    assert not ImageSearchIntent.objects.filter(pk=image_intent.pk).exists()
    assert not ImageSearchTask.objects.filter(pk=image_task.pk).exists()
    assert ImageSearchIntent.objects.filter(pk=active_image_intent.pk).exists()
    assert ImageSearchTask.objects.filter(pk=active_image_task.pk).exists()
    assert not ProductParseIntent.objects.filter(pk=parse_intent.pk).exists()
    assert not ProductParseJob.objects.filter(pk=parse_job.pk).exists()
    assert ProductParseIntent.objects.filter(pk=active_parse_intent.pk).exists()
    assert ProductParseJob.objects.filter(pk=active_parse_job.pk).exists()
    assert ProductParseIntent.objects.filter(pk=retained_intent.pk).exists()
    assert ProductParseJob.objects.filter(pk=retained_job.pk).exists()


@pytest.mark.django_db
def test_retention_keeps_image_dispatch_until_its_intent_expires(settings):
    import uuid

    from apps.core.models import BackgroundJobDispatch
    from apps.image_search.models import ImageSearchIntent, ImageSearchTask
    from apps.products.models import Product

    settings.BACKGROUND_JOB_RETENTION_DAYS = 1
    settings.IMAGE_SEARCH_TASK_RETENTION_DAYS = 30
    settings.RETENTION_PURGE_BATCH_SIZE = 100
    tenant, _ = TenantService.create_tenant(
        'Dispatch retention',
        'dispatch-retention',
        'dispatch-retention@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='DISPATCH-RETENTION',
        name='Dispatch retention product',
        price='1.00',
    )
    intent = ImageSearchIntent.objects.create(
        tenant=tenant,
        operation=ImageSearchIntent.Operation.SINGLE,
        idempotency_key=uuid.uuid4(),
        request_fingerprint='f' * 64,
        request_payload={'product_id': product.pk},
    )
    dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.image_search.tasks.search_images_for_product',
        queue='image_search',
        status=BackgroundJobDispatch.Status.SUCCEEDED,
        finished_at=timezone.now() - timedelta(days=2),
    )
    task = ImageSearchTask.objects.create(
        tenant=tenant,
        product=product,
        intent=intent,
        dispatch=dispatch,
        task_id='dispatch-retention-task',
    )

    first = purge_retained_data()

    assert first['background_job_dispatches'] == 0
    assert BackgroundJobDispatch.objects.filter(pk=dispatch.pk).exists()
    assert ImageSearchIntent.objects.filter(pk=intent.pk).exists()

    expired = timezone.now() - timedelta(days=31)
    ImageSearchIntent.objects.filter(pk=intent.pk).update(created_at=expired)
    ImageSearchTask.objects.filter(pk=task.pk).update(created_at=expired)
    second = purge_retained_data()

    assert second['image_search_intents'] == 1
    assert not ImageSearchIntent.objects.filter(pk=intent.pk).exists()
    assert BackgroundJobDispatch.objects.filter(pk=dispatch.pk).exists()

    third = purge_retained_data()
    assert third['background_job_dispatches'] == 1
    assert not BackgroundJobDispatch.objects.filter(pk=dispatch.pk).exists()


@pytest.mark.django_db
def test_retention_never_deletes_unresolved_media_or_its_soft_deleted_product(settings):
    from apps.media_processing.models import MediaProcessingJob
    from apps.products.models import Product, ProductImage

    settings.SOFT_DELETE_RETENTION_DAYS = 1
    settings.MEDIA_PROCESSING_JOB_RETENTION_DAYS = 1
    settings.RETENTION_PURGE_BATCH_SIZE = 100
    tenant, _ = TenantService.create_tenant(
        'Uncertain media retention',
        'uncertain-media-retention',
        'uncertain-media-retention@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='UNCERTAIN-1',
        name='Uncertain media product',
        price='1.00',
    )
    image = ProductImage.objects.create(
        product=product,
        s3_key='dev/products/uncertain/source.jpg',
        sha256='uncertain-media-source',
    )
    job = MediaProcessingJob.objects.create(
        tenant=tenant,
        product_image=image,
        operations=['resize'],
        status=MediaProcessingJob.Status.FAILED,
        error_code='outcome_uncertain',
        provider_metadata={
            'credit_reservation': {
                'status': 'reserved',
                'key': 'media-job:uncertain:credits:1',
                'amount': '2',
            },
        },
    )
    expired = timezone.now() - timedelta(days=2)
    Product.all_objects.filter(pk=product.pk).update(deleted_at=expired)
    MediaProcessingJob.objects.filter(pk=job.pk).update(
        created_at=expired,
        updated_at=expired,
    )

    result = purge_retained_data()

    assert result['products'] == 0
    assert result['media_processing_jobs'] == 0
    assert Product.all_objects.filter(pk=product.pk).exists()
    assert MediaProcessingJob.objects.filter(pk=job.pk).exists()


@pytest.mark.django_db
def test_ai_provider_retention_never_purges_unapplied_or_uncertain_results(settings):
    from apps.ai_agent.models import AIProviderOperation, AITaskType

    settings.AI_PROVIDER_OPERATION_RETENTION_DAYS = 30
    settings.RETENTION_PURGE_BATCH_SIZE = 1000
    tenant, _ = TenantService.create_tenant(
        'AI Operation Retention',
        'ai-operation-retention',
        'ai-operation-retention@example.com',
        'pass12345',
    )
    old = timezone.now() - timedelta(days=31)
    common = {
        'tenant': tenant,
        'task_type': AITaskType.DESCRIPTION,
        'provider': 'openai',
        'model_id': 'test-model',
        'reserved_amount': Decimal('1'),
        'domain_type': AIProviderOperation.DomainType.PRODUCT,
        'domain_reference': '42',
    }
    applied = AIProviderOperation.objects.create(
        **common,
        reservation_key='retention:applied',
        status=AIProviderOperation.Status.SETTLED,
        apply_state=AIProviderOperation.ApplyState.APPLIED,
        resolved_at=old,
        applied_at=old,
        validated_result={'title': 'done'},
    )
    unapplied = AIProviderOperation.objects.create(
        **common,
        reservation_key='retention:unapplied',
        status=AIProviderOperation.Status.SETTLED,
        apply_state=AIProviderOperation.ApplyState.PENDING,
        resolved_at=old,
        validated_result={'title': 'must survive'},
    )
    uncertain = AIProviderOperation.objects.create(
        **{**common, 'domain_reference': '43'},
        reservation_key='retention:uncertain',
        status=AIProviderOperation.Status.PENDING_RECONCILIATION,
        apply_state=AIProviderOperation.ApplyState.NOT_REQUIRED,
        uncertainty_marked_at=old,
    )

    result = purge_retained_data()

    assert result['ai_provider_operations'] == 1
    assert not AIProviderOperation.objects.filter(pk=applied.pk).exists()
    assert AIProviderOperation.objects.filter(pk=unapplied.pk).exists()
    assert AIProviderOperation.objects.filter(pk=uncertain.pk).exists()


@pytest.mark.django_db
def test_unresolved_ai_owner_subquery_is_lazy_and_ignores_malformed_backlog():
    from apps.ai_agent.models import AIProviderOperation, AITaskType
    from apps.ai_agent.protection import unresolved_ai_provider_operation_q
    from apps.core.retention import _numeric_ai_domain_reference_ids

    tenant, _ = TenantService.create_tenant(
        'AI lazy retention fence',
        'ai-lazy-retention-fence',
        'ai-lazy-retention-fence@example.com',
        'pass12345',
    )
    operations = [
        AIProviderOperation(
            tenant=tenant,
            task_type=AITaskType.DESCRIPTION,
            provider='openai',
            model_id='retention-model',
            reservation_key=f'lazy-retention:{index}',
            reserved_amount=Decimal('1'),
            domain_type=AIProviderOperation.DomainType.PRODUCT,
            domain_reference=str(100_000 + index),
            status=AIProviderOperation.Status.PENDING_RECONCILIATION,
        )
        for index in range(250)
    ]
    operations.extend([
        AIProviderOperation(
            tenant=tenant,
            task_type=AITaskType.DESCRIPTION,
            provider='openai',
            model_id='retention-model',
            reservation_key='lazy-retention:malformed',
            reserved_amount=Decimal('1'),
            domain_type=AIProviderOperation.DomainType.PRODUCT,
            domain_reference='not-a-database-id',
            status=AIProviderOperation.Status.PENDING_RECONCILIATION,
        ),
        AIProviderOperation(
            tenant=tenant,
            task_type=AITaskType.DESCRIPTION,
            provider='openai',
            model_id='retention-model',
            reservation_key='lazy-retention:oversized',
            reserved_amount=Decimal('1'),
            domain_type=AIProviderOperation.DomainType.PRODUCT,
            domain_reference='9' * 100,
            status=AIProviderOperation.Status.PENDING_RECONCILIATION,
        ),
    ])
    AIProviderOperation.objects.bulk_create(operations)
    unresolved = AIProviderOperation.objects.filter(
        unresolved_ai_provider_operation_q(),
    )

    with CaptureQueriesContext(connection) as captured:
        owner_ids = _numeric_ai_domain_reference_ids(
            unresolved,
            AIProviderOperation.DomainType.PRODUCT,
        )

    assert len(captured) == 0
    assert list(owner_ids.order_by('_numeric_owner_id')[:3]) == [
        {'_numeric_owner_id': 100_000},
        {'_numeric_owner_id': 100_001},
        {'_numeric_owner_id': 100_002},
    ]
    assert owner_ids.count() == 250


@pytest.mark.django_db
def test_retention_keeps_ai_operation_domain_owners_until_terminal_apply(settings):
    from apps.ai_agent.models import AIProviderOperation, AITaskType
    from apps.products.models import Product
    from apps.web_research.models import WebResearchRun

    settings.SOFT_DELETE_RETENTION_DAYS = 1
    settings.AI_PROVIDER_OPERATION_RETENTION_DAYS = 1
    settings.RETENTION_PURGE_BATCH_SIZE = 100
    tenant, _ = TenantService.create_tenant(
        'AI owner retention',
        'ai-owner-retention',
        'ai-owner-retention@example.com',
        'pass12345',
    )
    description_product = Product.objects.create(
        tenant=tenant,
        article='AI-OWNER-DESCRIPTION',
        name='Description owner',
        price='1.00',
    )
    research_product = Product.objects.create(
        tenant=tenant,
        article='AI-OWNER-RESEARCH',
        name='Research owner',
        price='1.00',
    )
    run = WebResearchRun.objects.create(
        tenant=tenant,
        product=research_product,
    )
    description_operation = AIProviderOperation.objects.create(
        tenant=tenant,
        task_type=AITaskType.DESCRIPTION,
        provider='openai',
        model_id='retention-model',
        reservation_key='retention:description-owner',
        reserved_amount=Decimal('1'),
        domain_type=AIProviderOperation.DomainType.PRODUCT,
        domain_reference=str(description_product.pk),
        status=AIProviderOperation.Status.SETTLED,
        apply_state=AIProviderOperation.ApplyState.PENDING,
        validated_result={'title': 'Paid result'},
    )
    research_operation = AIProviderOperation.objects.create(
        tenant=tenant,
        task_type=AITaskType.WEB_RESEARCH,
        provider='openai',
        model_id='retention-model',
        reservation_key='retention:research-owner',
        reserved_amount=Decimal('1'),
        domain_type=AIProviderOperation.DomainType.WEB_RESEARCH_RUN,
        domain_reference=str(run.pk),
        status=AIProviderOperation.Status.PENDING_RECONCILIATION,
        apply_state=AIProviderOperation.ApplyState.NOT_REQUIRED,
    )
    expired = timezone.now() - timedelta(days=2)
    Product.all_objects.filter(
        pk__in=[description_product.pk, research_product.pk],
    ).update(deleted_at=expired)

    retained = purge_retained_data()

    assert retained['products'] == 0
    assert Product.all_objects.filter(pk=description_product.pk).exists()
    assert Product.all_objects.filter(pk=research_product.pk).exists()
    assert WebResearchRun.objects.filter(pk=run.pk).exists()

    description_operation.apply_state = AIProviderOperation.ApplyState.APPLIED
    description_operation.applied_at = expired
    description_operation.resolved_at = expired
    description_operation.save(update_fields=[
        'apply_state', 'applied_at', 'resolved_at', 'updated_at',
    ])
    research_operation.status = AIProviderOperation.Status.RELEASED
    research_operation.released_at = expired
    research_operation.resolved_at = expired
    research_operation.save(update_fields=[
        'status', 'released_at', 'resolved_at', 'updated_at',
    ])

    purged = purge_retained_data()

    assert purged['products'] == 2
    assert not Product.all_objects.filter(pk=description_product.pk).exists()
    assert not Product.all_objects.filter(pk=research_product.pk).exists()
    assert not WebResearchRun.objects.filter(pk=run.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize('workflow_status', ['apply_pending', 'uncertain'])
def test_retention_keeps_image_owner_while_paid_workflow_active(
    settings,
    workflow_status,
):
    import uuid

    from apps.core.models import BackgroundJobDispatch
    from apps.image_search.models import ImageSearchIntent, ImageSearchTask
    from apps.products.models import Product
    from apps.web_research.models import WebSearchWorkflow

    settings.IMAGE_SEARCH_TASK_RETENTION_DAYS = 30
    settings.RETENTION_PURGE_BATCH_SIZE = 100
    tenant, _ = TenantService.create_tenant(
        f'Image workflow retention {workflow_status}',
        f'image-workflow-retention-{workflow_status}',
        f'image-workflow-retention-{workflow_status}@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article=f'IMAGE-{workflow_status}',
        name='Image workflow owner',
        price='1.00',
    )
    intent = ImageSearchIntent.objects.create(
        tenant=tenant,
        operation=ImageSearchIntent.Operation.SINGLE,
        idempotency_key=uuid.uuid4(),
        request_fingerprint='e' * 64,
        request_payload={'product_id': product.pk},
    )
    dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.image_search.tasks.search_images_for_product',
        queue='image_search',
        status=BackgroundJobDispatch.Status.FAILED,
        finished_at=timezone.now(),
    )
    task = ImageSearchTask.objects.create(
        tenant=tenant,
        product=product,
        intent=intent,
        dispatch=dispatch,
        task_id=f'image-workflow-{workflow_status}',
        status=ImageSearchTask.Status.FAILED,
    )
    workflow = WebSearchWorkflow.objects.create(
        tenant=tenant,
        product=product,
        operation='image_search',
        domain_reference=f'product:{tenant.pk}:{product.pk}',
        workflow_key=f'image-search-task:{task.pk}',
        input_fingerprint='f' * 64,
        input_snapshot={'version': 1},
        status=workflow_status,
    )
    expired = timezone.now() - timedelta(days=31)
    ImageSearchIntent.objects.filter(pk=intent.pk).update(created_at=expired)
    ImageSearchTask.objects.filter(pk=task.pk).update(created_at=expired)

    retained = purge_retained_data()

    assert retained['image_search_intents'] == 0
    assert ImageSearchIntent.objects.filter(pk=intent.pk).exists()
    assert ImageSearchTask.objects.filter(pk=task.pk).exists()

    workflow.status = WebSearchWorkflow.Status.APPLIED
    workflow.applied_at = expired
    workflow.product = None
    workflow.save(update_fields=['status', 'applied_at', 'product', 'updated_at'])
    purged = purge_retained_data()

    assert purged['image_search_intents'] == 1
    assert not ImageSearchIntent.objects.filter(pk=intent.pk).exists()
    assert not ImageSearchTask.objects.filter(pk=task.pk).exists()
