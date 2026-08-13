from datetime import timedelta

from django.conf import settings
from django.db.models import (
    BigIntegerField, Case, CharField, Exists, OuterRef, Q, Value, When,
)
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Concat
from django.utils import timezone


def _bounded_queryset(queryset, batch_size: int):
    """Return at most one deterministic purge batch without slicing delete()."""
    pks = list(queryset.order_by('pk').values_list('pk', flat=True)[:batch_size])
    return queryset.filter(pk__in=pks), len(pks)


def _numeric_ai_domain_reference_ids(queryset, domain_type: str):
    """Lazily select safe integer owner references entirely in the database.

    The 18-digit bound is below signed BIGINT overflow. Malformed/oversized
    audit references stay retained as operations but cannot crash or force an
    unbounded Python materialization during every retention pass.
    """
    return queryset.filter(
        domain_type=domain_type,
        domain_reference__regex=r'^[0-9]{1,18}$',
    ).annotate(
        _numeric_owner_id=Cast(
            'domain_reference',
            output_field=BigIntegerField(),
        ),
    ).values('_numeric_owner_id')


def purge_retained_data(*, dry_run: bool = False) -> dict[str, int]:
    """Purge one bounded batch whose soft-delete/audit retention expired."""
    from apps.ai_agent.models import AIProviderOperation
    from apps.ai_agent.protection import unresolved_ai_provider_operation_q
    from apps.billing.models import BillingOutboxEvent, BillingWebhookEvent
    from apps.core.models import (
        BackgroundJobDispatch, PaidIngressIntent, TenantDailyPaidUsage,
    )
    from apps.datasources.models import DataSourceConnection
    from apps.image_search.models import (
        ImageSearchCache,
        ImageSearchIntent,
        ImageSearchLog,
        ImageSearchTask,
    )
    from apps.marketplaces.models import Listing, MarketplaceAccount
    from apps.media_processing.models import MediaProcessingJob
    from apps.media_processing.protection import unresolved_media_job_q
    from apps.notifications.models import NotificationDelivery
    from apps.products.models import (
        Product,
        ProductBulkActionJob,
        ProductParseIntent,
        ProductParseJob,
    )
    from apps.sync.models import SyncLog
    from apps.tenants.models import WebhookDelivery, WebhookEndpoint, WebhookEvent
    from apps.web_research.models import (
        WebResearchRun,
        WebSearchAttempt,
        WebSearchWorkflow,
    )
    from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

    now = timezone.now()
    soft_cutoff = now - timedelta(days=settings.SOFT_DELETE_RETENTION_DAYS)
    webhook_cutoff = now - timedelta(days=settings.WEBHOOK_AUDIT_RETENTION_DAYS)
    billing_cutoff = now - timedelta(days=settings.BILLING_AUDIT_RETENTION_DAYS)
    sync_cutoff = now - timedelta(days=settings.SYNC_LOG_RETENTION_DAYS)
    notification_delivery_cutoff = now - timedelta(
        days=settings.NOTIFICATION_DELIVERY_RETENTION_DAYS,
    )
    parse_raw_html_cutoff = now - timedelta(
        days=settings.PRODUCT_PARSE_RAW_HTML_RETENTION_DAYS,
    )
    parse_job_cutoff = now - timedelta(days=settings.PRODUCT_PARSE_JOB_RETENTION_DAYS)
    image_log_cutoff = now - timedelta(days=settings.IMAGE_SEARCH_LOG_RETENTION_DAYS)
    image_task_cutoff = now - timedelta(days=settings.IMAGE_SEARCH_TASK_RETENTION_DAYS)
    bulk_job_cutoff = now - timedelta(
        days=settings.PRODUCT_BULK_ACTION_JOB_RETENTION_DAYS,
    )
    media_job_cutoff = now - timedelta(
        days=settings.MEDIA_PROCESSING_JOB_RETENTION_DAYS,
    )
    background_job_cutoff = now - timedelta(days=settings.BACKGROUND_JOB_RETENTION_DAYS)
    ai_provider_operation_cutoff = now - timedelta(
        days=settings.AI_PROVIDER_OPERATION_RETENTION_DAYS,
    )
    web_search_attempt_cutoff = now - timedelta(
        days=settings.WEB_SEARCH_ATTEMPT_RETENTION_DAYS,
    )
    active_web_workflows = WebSearchWorkflow.objects.filter(
        status__in=WebSearchWorkflow.ACTIVE_STATUSES,
    )
    active_web_product_ids = active_web_workflows.exclude(
        product_id__isnull=True,
    ).values_list('product_id', flat=True)
    replayable_applied_web_workflows = WebSearchWorkflow.objects.filter(
        operation='web_research',
        status=WebSearchWorkflow.Status.APPLIED,
        run__status__in=[
            WebResearchRun.Status.QUEUED,
            WebResearchRun.Status.RUNNING,
            WebResearchRun.Status.FAILED,
        ],
    ).filter(
        Q(
            attempts__status__in=[
                WebSearchAttempt.Status.SUCCESS,
                WebSearchAttempt.Status.EMPTY,
            ],
            attempts__checkpoint_enc__isnull=False,
        )
        | Q(
            attempts__isnull=True,
            run__status__in=[
                WebResearchRun.Status.QUEUED,
                WebResearchRun.Status.RUNNING,
            ],
        )
    )
    replayable_applied_web_product_ids = (
        replayable_applied_web_workflows.values_list(
            'run__product_id', flat=True,
        )
    )
    active_image_owner = active_web_workflows.filter(
        operation='image_search',
        workflow_key=Concat(
            Value('image-search-task:'),
            Cast(OuterRef('pk'), output_field=CharField()),
        ),
    )
    active_parse_owner = active_web_workflows.filter(
        operation='euroauto',
        workflow_key=Concat(
            Value('product-parse-job:'),
            Cast(OuterRef('pk'), output_field=CharField()),
        ),
    )
    active_parse_job_workflow = active_web_workflows.filter(
        operation='euroauto',
        workflow_key=Concat(
            Value('product-parse-job:'),
            Cast(OuterRef('pk'), output_field=CharField()),
        ),
    )
    image_tasks_with_workflow = ImageSearchTask.objects.annotate(
        _has_active_web_workflow=Exists(active_image_owner),
    ).filter(_has_active_web_workflow=True)
    parse_jobs_with_workflow = ProductParseJob.objects.annotate(
        _has_active_web_workflow=Exists(active_parse_owner),
    ).filter(_has_active_web_workflow=True)
    active_image_task_ids = image_tasks_with_workflow.values_list('pk', flat=True)
    active_parse_job_ids = parse_jobs_with_workflow.values_list('pk', flat=True)
    active_image_dispatch_ids = image_tasks_with_workflow.exclude(
        dispatch_id__isnull=True,
    ).values_list('dispatch_id', flat=True)
    # Dispatch args are JSON. Guard the cast so a corrupted/non-numeric row
    # cannot make an otherwise bounded retention pass fail.
    parse_dispatches = BackgroundJobDispatch.objects.filter(
        task_name__in=[
            'apps.products.tasks.parse_single_part',
            'apps.products.tasks.parse_single_part_then_generate_description',
        ],
    ).annotate(
        _parse_owner_id=Case(
            When(
                args__0__regex=r'^[0-9]{1,18}$',
                then=Cast(
                    KeyTextTransform('0', 'args'),
                    output_field=BigIntegerField(),
                ),
            ),
            default=Value(None),
            output_field=BigIntegerField(),
        ),
    )
    active_parse_dispatch_ids = parse_dispatches.filter(
        _parse_owner_id__in=active_parse_job_ids,
    ).values_list('pk', flat=True)
    parse_jobs_with_active_workflow = ProductParseJob.objects.filter(
        ingress_intent_id=OuterRef('pk'),
    ).annotate(
        _has_active_web_workflow=Exists(active_parse_job_workflow),
    ).filter(_has_active_web_workflow=True)
    parse_intents_with_workflow = ProductParseIntent.objects.annotate(
        _has_active_web_workflow=Exists(parse_jobs_with_active_workflow),
    ).filter(_has_active_web_workflow=True)
    active_parse_intent_ids = parse_intents_with_workflow.values_list(
        'pk', flat=True,
    )

    unresolved_ai_operations = AIProviderOperation.objects.filter(
        unresolved_ai_provider_operation_q(),
    )

    unresolved_ai_product_ids = _numeric_ai_domain_reference_ids(
        unresolved_ai_operations,
        AIProviderOperation.DomainType.PRODUCT,
    )
    unresolved_ai_run_ids = _numeric_ai_domain_reference_ids(
        unresolved_ai_operations,
        AIProviderOperation.DomainType.WEB_RESEARCH_RUN,
    )
    unresolved_ai_run_product_ids = WebResearchRun.objects.filter(
        pk__in=unresolved_ai_run_ids,
    ).values_list('product_id', flat=True)
    terminal_parse_statuses = [
        ProductParseJob.Status.SUCCESS,
        ProductParseJob.Status.FAILED,
        ProductParseJob.Status.NOT_FOUND,
        ProductParseJob.Status.NEED_REVIEW,
    ]
    terminal_bulk_statuses = [
        ProductBulkActionJob.Status.SUCCESS,
        ProductBulkActionJob.Status.FAILED,
        ProductBulkActionJob.Status.CANCELLED,
    ]
    terminal_media_statuses = [
        MediaProcessingJob.Status.SUCCEEDED,
        MediaProcessingJob.Status.FAILED,
        MediaProcessingJob.Status.CANCELLED,
    ]
    unresolved_media_jobs = MediaProcessingJob.objects.filter(
        unresolved_media_job_q(),
    )
    unresolved_media_product_ids = unresolved_media_jobs.values_list(
        'product_image__product_id', flat=True,
    )
    terminal_background_statuses = [
        BackgroundJobDispatch.Status.SUCCEEDED,
        BackgroundJobDispatch.Status.FAILED,
        BackgroundJobDispatch.Status.CANCELLED,
    ]
    nonterminal_image_intent_ids = ImageSearchTask.objects.filter(
        intent__isnull=False,
    ).filter(
        Q(pk__in=active_image_task_ids)
        | Q(dispatch__isnull=True)
        | ~Q(dispatch__status__in=terminal_background_statuses)
    ).values_list('intent_id', flat=True)
    retained_parse_intent_job_ids = ProductParseIntent.objects.filter(
        created_at__gte=parse_job_cutoff,
        primary_job__isnull=False,
    ).values_list('primary_job_id', flat=True)
    retained_paid_dispatch_ids = PaidIngressIntent.objects.filter(
        created_at__gte=background_job_cutoff,
        dispatch__isnull=False,
    ).values_list('dispatch_id', flat=True)
    querysets = {
        'listings': Listing.all_objects.filter(deleted_at__lt=soft_cutoff),
        'products': Product.all_objects.filter(
            deleted_at__lt=soft_cutoff,
        ).exclude(pk__in=unresolved_media_product_ids).exclude(
            pk__in=active_web_product_ids,
        ).exclude(
            pk__in=replayable_applied_web_product_ids,
        ).exclude(
            pk__in=unresolved_ai_product_ids,
        ).exclude(
            pk__in=unresolved_ai_run_product_ids,
        ),
        'marketplace_accounts': MarketplaceAccount.all_objects.filter(deleted_at__lt=soft_cutoff),
        'datasource_connections': DataSourceConnection.all_objects.filter(deleted_at__lt=soft_cutoff),
        'webhook_endpoints': WebhookEndpoint.all_objects.filter(deleted_at__lt=soft_cutoff),
        'webhook_events': WebhookEvent.objects.filter(created_at__lt=webhook_cutoff).exclude(
            deliveries__status__in=[
                WebhookDelivery.STATUS_PENDING,
                WebhookDelivery.STATUS_QUEUED,
                WebhookDelivery.STATUS_RETRY,
                WebhookDelivery.STATUS_DELIVERING,
            ],
        ),
        'billing_webhook_events': BillingWebhookEvent.objects.filter(
            created_at__lt=billing_cutoff,
            processed_at__isnull=False,
        ).exclude(decision=BillingWebhookEvent.DECISION_MANUAL_REVIEW),
        'billing_outbox_events': BillingOutboxEvent.objects.filter(
            status=BillingOutboxEvent.STATUS_DISPATCHED,
            dispatched_at__lt=billing_cutoff,
        ),
        'sync_logs': SyncLog.objects.filter(created_at__lt=sync_cutoff),
        'notification_deliveries': NotificationDelivery.objects.filter(
            status__in=NotificationDelivery.TERMINAL_RETENTION_STATUSES,
            updated_at__lt=notification_delivery_cutoff,
        ),
        'product_parse_intents': ProductParseIntent.objects.filter(
            created_at__lt=parse_job_cutoff,
            primary_job__isnull=False,
        ).exclude(jobs__status__in=[
            ProductParseJob.Status.PENDING,
            ProductParseJob.Status.RUNNING,
        ]).exclude(pk__in=active_parse_intent_ids),
        'product_parse_jobs': ProductParseJob.objects.filter(
            created_at__lt=parse_job_cutoff,
            status__in=terminal_parse_statuses,
            ingress_intent__isnull=True,
        ).exclude(pk__in=retained_parse_intent_job_ids).exclude(
            pk__in=active_parse_job_ids,
        ),
        'image_search_logs': ImageSearchLog.objects.filter(created_at__lt=image_log_cutoff),
        'image_search_intents': ImageSearchIntent.objects.filter(
            created_at__lt=image_task_cutoff,
        ).exclude(pk__in=nonterminal_image_intent_ids),
        'image_search_tasks': ImageSearchTask.objects.filter(
            created_at__lt=image_task_cutoff,
            intent__isnull=True,
        ).exclude(pk__in=active_image_task_ids),
        'expired_image_search_cache': ImageSearchCache.objects.filter(expires_at__lt=now),
        'product_bulk_action_jobs': ProductBulkActionJob.objects.filter(
            created_at__lt=bulk_job_cutoff,
            status__in=terminal_bulk_statuses,
        ),
        'media_processing_jobs': MediaProcessingJob.objects.filter(
            created_at__lt=media_job_cutoff,
            status__in=terminal_media_statuses,
        ).exclude(pk__in=unresolved_media_jobs.values('pk')),
        'background_job_dispatches': BackgroundJobDispatch.objects.filter(
            finished_at__lt=background_job_cutoff,
            status__in=terminal_background_statuses,
        ).exclude(image_search_requests__intent__isnull=False).exclude(
            pk__in=retained_paid_dispatch_ids,
        ).exclude(
            pk__in=active_image_dispatch_ids,
        ).exclude(
            pk__in=active_parse_dispatch_ids,
        ),
        'paid_ingress_intents': PaidIngressIntent.objects.filter(
            created_at__lt=background_job_cutoff,
        ).exclude(
            dispatch__isnull=False,
            dispatch__status__in=[
                BackgroundJobDispatch.Status.PENDING,
                BackgroundJobDispatch.Status.PUBLISHING,
                BackgroundJobDispatch.Status.PUBLISHED,
                BackgroundJobDispatch.Status.RUNNING,
            ],
        ),
        'tenant_daily_paid_usage': TenantDailyPaidUsage.objects.filter(
            usage_date__lt=background_job_cutoff.date(),
        ),
        'web_search_attempts': WebSearchAttempt.objects.filter(
            Q(
                reconciliation_state=(
                    WebSearchAttempt.ReconciliationState.NOT_REQUIRED
                ),
                updated_at__lt=web_search_attempt_cutoff,
                status__in=[
                    WebSearchAttempt.Status.SUCCESS,
                    WebSearchAttempt.Status.EMPTY,
                    WebSearchAttempt.Status.FAILED,
                    WebSearchAttempt.Status.SKIPPED,
                ],
                apply_state=WebSearchAttempt.ApplyState.APPLIED,
            )
            | Q(
                reconciliation_state=(
                    WebSearchAttempt.ReconciliationState.RESOLVED
                ),
                reconciled_at__lt=web_search_attempt_cutoff,
                apply_state=WebSearchAttempt.ApplyState.APPLIED,
            )
        ).exclude(
            workflow__in=replayable_applied_web_workflows,
        ),
        'web_search_workflows': WebSearchWorkflow.objects.filter(
            Q(
                status=WebSearchWorkflow.Status.APPLIED,
                applied_at__lt=web_search_attempt_cutoff,
            )
            | Q(
                status=WebSearchWorkflow.Status.RECONCILED,
                reconciled_at__lt=web_search_attempt_cutoff,
            )
        ).filter(attempts__isnull=True).exclude(
            pk__in=replayable_applied_web_workflows.values('pk'),
        ),
        # Never purge held reservations, uncertain outcomes, or paid results
        # that have not yet committed their domain writes.
        'ai_provider_operations': AIProviderOperation.objects.filter(
            Q(
                status=AIProviderOperation.Status.RELEASED,
                resolved_at__lt=ai_provider_operation_cutoff,
                apply_state=AIProviderOperation.ApplyState.NOT_REQUIRED,
            )
            | Q(
                status=AIProviderOperation.Status.SETTLED,
                resolved_at__lt=ai_provider_operation_cutoff,
                apply_state=AIProviderOperation.ApplyState.NOT_REQUIRED,
            )
            | Q(
                status=AIProviderOperation.Status.SETTLED,
                applied_at__lt=ai_provider_operation_cutoff,
                apply_state=AIProviderOperation.ApplyState.APPLIED,
            )
        ),
        # Удаление OutstandingToken каскадно удаляет BlacklistedToken. Хранить
        # истёкшие JWT дольше их cryptographic lifetime нет оснований.
        'expired_jwt_tokens': OutstandingToken.objects.filter(expires_at__lt=now),
    }

    raw_html_queryset = ProductParseJob.objects.filter(
        created_at__lt=parse_raw_html_cutoff,
        status__in=terminal_parse_statuses,
    ).exclude(raw_html='')
    batch_size = settings.RETENTION_PURGE_BATCH_SIZE
    bounded_querysets = {}
    result = {}
    for name, queryset in querysets.items():
        bounded_querysets[name], result[name] = _bounded_queryset(queryset, batch_size)
    bounded_raw_html, result['product_parse_raw_html'] = _bounded_queryset(
        raw_html_queryset,
        batch_size,
    )

    if dry_run:
        return result
    bounded_raw_html.update(raw_html='')
    # Сначала дочерние записи, затем их владельцы.
    for name in (
        'image_search_logs', 'image_search_intents', 'image_search_tasks',
        'expired_image_search_cache', 'product_bulk_action_jobs',
        'media_processing_jobs', 'paid_ingress_intents',
        'tenant_daily_paid_usage', 'web_search_attempts',
        'web_search_workflows',
        'background_job_dispatches',
        'ai_provider_operations',
        'product_parse_intents', 'product_parse_jobs',
        'listings', 'products', 'marketplace_accounts',
        'datasource_connections', 'webhook_endpoints', 'webhook_events',
        'billing_webhook_events', 'billing_outbox_events', 'sync_logs',
        'notification_deliveries',
        'expired_jwt_tokens',
    ):
        bounded_querysets[name].delete()
    return result
