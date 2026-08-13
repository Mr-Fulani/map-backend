from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils.timezone import now

from apps.core.dispatch import (
    claim_dispatch,
    enqueue_durable_task,
    execute_claimed_dispatch,
    publish_due_dispatches,
)
from apps.core.models import BackgroundJobDispatch


PARSE_TASK = 'apps.products.tasks.parse_single_part'
REPLAY_SAFE_TASK = 'apps.products.tasks.process_bulk_product_action'


@pytest.mark.django_db
def test_broker_publish_failure_keeps_durable_row_pending(
    django_capture_on_commit_callbacks,
):
    with patch(
        'apps.core.tasks.execute_background_dispatch.apply_async',
        side_effect=RuntimeError('broker unavailable'),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            dispatch = enqueue_durable_task(PARSE_TASK, args=[501])

    dispatch.refresh_from_db()
    assert dispatch.status == BackgroundJobDispatch.Status.PENDING
    assert dispatch.claim_token is None
    assert dispatch.lease_expires_at is None
    assert 'broker unavailable' in dispatch.last_error


@pytest.mark.django_db
def test_partial_fanout_failure_does_not_lose_unpublished_children(
    django_capture_on_commit_callbacks,
):
    with patch(
        'apps.core.tasks.execute_background_dispatch.apply_async',
        side_effect=[None, RuntimeError('second publish failed')],
    ):
        with django_capture_on_commit_callbacks(execute=True):
            first = enqueue_durable_task(
                PARSE_TASK,
                args=[601],
                deduplication_key='fanout:601',
            )
            second = enqueue_durable_task(
                PARSE_TASK,
                args=[602],
                deduplication_key='fanout:602',
            )

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.status == BackgroundJobDispatch.Status.PUBLISHED
    assert second.status == BackgroundJobDispatch.Status.PENDING

    second.available_at = now() - timedelta(seconds=1)
    second.save(update_fields=['available_at', 'updated_at'])
    with patch('apps.core.tasks.execute_background_dispatch.apply_async') as publish:
        result = publish_due_dispatches()

    second.refresh_from_db()
    assert result == {'selected': 1, 'published': 1}
    assert second.status == BackgroundJobDispatch.Status.PUBLISHED
    publish.assert_called_once()


@pytest.mark.django_db
def test_consumer_claim_is_idempotent_and_result_is_persistent(
    django_capture_on_commit_callbacks,
):
    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            dispatch = enqueue_durable_task(PARSE_TASK, args=[701])

    dispatch.refresh_from_db()
    claimed = claim_dispatch(dispatch.pk, dispatch.claim_token)
    assert claimed is not None
    assert claim_dispatch(dispatch.pk, dispatch.claim_token) is None

    target = SimpleNamespace(run=lambda job_id: {'job_id': job_id, 'ok': True})
    with patch(
        'apps.core.dispatch._registered_task',
        return_value=target,
    ):
        result = execute_claimed_dispatch(claimed)

    dispatch.refresh_from_db()
    assert result['status'] == 'succeeded'
    assert dispatch.status == BackgroundJobDispatch.Status.SUCCEEDED
    assert dispatch.result == {'job_id': 701, 'ok': True}


@pytest.mark.django_db
def test_expired_worker_lease_is_republished_with_new_claim_token():
    dispatch = BackgroundJobDispatch.objects.create(
        task_name=REPLAY_SAFE_TASK,
        queue='part_parsing_bulk',
        args=[801],
        status=BackgroundJobDispatch.Status.RUNNING,
        claim_token='1a13ba4d-32b5-40d0-92ca-a4401016b6c5',
        lease_expires_at=now() - timedelta(seconds=1),
        run_attempts=1,
    )
    old_claim = dispatch.claim_token

    with patch('apps.core.tasks.execute_background_dispatch.apply_async') as publish:
        result = publish_due_dispatches()

    dispatch.refresh_from_db()
    assert result == {'selected': 1, 'published': 1}
    assert dispatch.status == BackgroundJobDispatch.Status.PUBLISHED
    assert dispatch.claim_token != old_claim
    publish.assert_called_once()


@pytest.mark.django_db
@pytest.mark.parametrize('task_name,queue', [
    ('apps.ai_agent.tasks.generate_description_task', 'ai_generate'),
])
def test_expired_external_effect_is_failed_without_replay(task_name, queue):
    dispatch = BackgroundJobDispatch.objects.create(
        task_name=task_name,
        queue=queue,
        args=[802],
        status=BackgroundJobDispatch.Status.RUNNING,
        claim_token='1a13ba4d-32b5-40d0-92ca-a4401016b6c5',
        lease_expires_at=now() - timedelta(seconds=1),
        run_attempts=1,
    )

    with patch('apps.core.tasks.execute_background_dispatch.apply_async') as publish:
        result = publish_due_dispatches()

    dispatch.refresh_from_db()
    assert result == {'selected': 1, 'published': 0}
    assert dispatch.status == BackgroundJobDispatch.Status.FAILED
    assert dispatch.finished_at is not None
    assert dispatch.claim_token is None
    assert 'автоматический повтор запрещён' in dispatch.last_error
    assert dispatch.result['reason_code'] == 'outcome_uncertain'
    publish.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize('task_name,queue,args', [
    (
        'apps.web_research.tasks.run_web_research',
        'part_parsing',
        [802],
    ),
    (
        'apps.image_search.tasks.search_images_for_product',
        'image_search',
        [802, 902],
    ),
    ('apps.products.tasks.parse_single_part', 'part_parsing', [802]),
    (
        'apps.products.tasks.parse_single_part_then_generate_description',
        'part_parsing',
        [802],
    ),
])
def test_expired_checkpointed_workflow_owner_is_republished(
    task_name,
    queue,
    args,
):
    dispatch = BackgroundJobDispatch.objects.create(
        task_name=task_name,
        queue=queue,
        args=args,
        status=BackgroundJobDispatch.Status.RUNNING,
        claim_token='1a13ba4d-32b5-40d0-92ca-a4401016b6c5',
        lease_expires_at=now() - timedelta(seconds=1),
        run_attempts=1,
    )

    with patch('apps.core.tasks.execute_background_dispatch.apply_async') as publish:
        result = publish_due_dispatches()

    dispatch.refresh_from_db()
    assert result == {'selected': 1, 'published': 1}
    assert dispatch.status == BackgroundJobDispatch.Status.PUBLISHED
    publish.assert_called_once()


@pytest.mark.django_db
def test_expired_image_dispatch_preserves_durable_domain_success():
    from apps.image_search.models import ImageSearchTask
    from apps.products.models import Product
    from apps.tenants.services import TenantService

    tenant, _ = TenantService.create_tenant(
        'Image lease ACK window',
        'image-lease-ack-window',
        'image-lease-ack-window@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='IMAGE-LEASE-ACK',
        name='Image lease ACK result',
        price='1.00',
    )
    tracking = ImageSearchTask.objects.create(
        tenant=tenant,
        product=product,
        task_id='image-lease-ack-tracking',
        status=ImageSearchTask.Status.SUCCEEDED,
        result={'saved_count': 1, 'reason_code': 'found'},
        finished_at=now(),
    )
    dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.image_search.tasks.search_images_for_product',
        queue='image_search',
        args=[product.pk, tracking.pk],
        status=BackgroundJobDispatch.Status.RUNNING,
        claim_token='1a13ba4d-32b5-40d0-92ca-a4401016b6c5',
        lease_expires_at=now() - timedelta(seconds=1),
        run_attempts=1,
    )
    tracking.dispatch = dispatch
    tracking.save(update_fields=['dispatch', 'updated_at'])

    with patch('apps.core.tasks.execute_background_dispatch.apply_async') as publish:
        result = publish_due_dispatches()

    dispatch.refresh_from_db()
    tracking.refresh_from_db()
    assert result == {'selected': 1, 'published': 1}
    assert dispatch.status == BackgroundJobDispatch.Status.PUBLISHED
    assert tracking.status == ImageSearchTask.Status.SUCCEEDED
    assert tracking.result == {'saved_count': 1, 'reason_code': 'found'}
    publish.assert_called_once()


@pytest.mark.django_db
def test_old_media_dispatch_expiry_cannot_overwrite_recovered_success():
    from apps.media_processing.models import MediaProcessingJob
    from apps.products.models import Product, ProductImage
    from apps.tenants.services import TenantService

    tenant, _ = TenantService.create_tenant(
        'Recovered media lease',
        'recovered-media-lease',
        'recovered-media-lease@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='RECOVERED-MEDIA-1',
        name='Recovered media product',
        price='1.00',
    )
    image = ProductImage.objects.create(
        product=product,
        s3_key='products/media/recovered-source.jpg',
    )
    job = MediaProcessingJob.objects.create(
        tenant=tenant,
        product_image=image,
        operations=['resize'],
        status=MediaProcessingJob.Status.SUCCEEDED,
        provider_response_state=(
            MediaProcessingJob.ProviderResponseState.APPLIED
        ),
        finished_at=now(),
    )
    old_dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.media_processing.tasks.process_media_job',
        queue='media_processing',
        args=[job.pk],
        status=BackgroundJobDispatch.Status.RUNNING,
        claim_token='1a13ba4d-32b5-40d0-92ca-a4401016b6c5',
        lease_expires_at=now() - timedelta(seconds=1),
        run_attempts=1,
    )

    with patch('apps.core.tasks.execute_background_dispatch.apply_async') as publish:
        result = publish_due_dispatches()

    old_dispatch.refresh_from_db()
    job.refresh_from_db()
    assert result == {'selected': 1, 'published': 0}
    assert old_dispatch.status == BackgroundJobDispatch.Status.FAILED
    assert job.status == MediaProcessingJob.Status.SUCCEEDED
    assert job.error_code == ''
    assert (
        job.provider_response_state
        == MediaProcessingJob.ProviderResponseState.APPLIED
    )
    publish.assert_not_called()


@pytest.mark.django_db
def test_external_effect_exception_is_terminal_on_first_attempt():
    dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.ai_agent.tasks.generate_description_task',
        queue='ai_generate',
        args=[803],
        status=BackgroundJobDispatch.Status.RUNNING,
        claim_token='1a13ba4d-32b5-40d0-92ca-a4401016b6c5',
        run_attempts=1,
        max_run_attempts=4,
    )
    target = SimpleNamespace(
        run=lambda product_id: (_ for _ in ()).throw(RuntimeError('provider uncertain')),
    )

    with patch('apps.core.dispatch._registered_task', return_value=target):
        result = execute_claimed_dispatch(dispatch)

    dispatch.refresh_from_db()
    assert result['status'] == 'failed'
    assert dispatch.status == BackgroundJobDispatch.Status.FAILED
    assert dispatch.finished_at is not None
    assert dispatch.run_attempts == 1
    assert dispatch.result['reason_code'] == 'outcome_uncertain'


@pytest.mark.django_db
def test_explicit_provider_uncertainty_stops_otherwise_retryable_parse_task():
    class OutcomeUncertain(RuntimeError):
        outcome_uncertain = True

    dispatch = BackgroundJobDispatch.objects.create(
        task_name=PARSE_TASK,
        queue='part_parsing',
        args=[804],
        status=BackgroundJobDispatch.Status.RUNNING,
        claim_token='1a13ba4d-32b5-40d0-92ca-a4401016b6c5',
        run_attempts=1,
        max_run_attempts=4,
    )
    target = SimpleNamespace(
        run=lambda job_id: (_ for _ in ()).throw(OutcomeUncertain('paid search unknown')),
    )

    with patch('apps.core.dispatch._registered_task', return_value=target):
        result = execute_claimed_dispatch(dispatch)

    dispatch.refresh_from_db()
    assert result['status'] == 'failed'
    assert dispatch.status == BackgroundJobDispatch.Status.FAILED
    assert dispatch.run_attempts == 1
    assert dispatch.result['reason_code'] == 'outcome_uncertain'


@pytest.mark.django_db
def test_web_research_lock_contention_keeps_dispatch_pending():
    from unittest.mock import MagicMock

    from apps.products.models import Product
    from apps.tenants.services import TenantService
    from apps.web_research.models import WebResearchRun

    tenant, _ = TenantService.create_tenant(
        'Web dispatch contention',
        'web-dispatch-contention',
        'web-dispatch-contention@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='WEB-DISPATCH-CONTENTION',
        name='Web dispatch contention',
        price='1.00',
    )
    run = WebResearchRun.objects.create(tenant=tenant, product=product)
    dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.web_research.tasks.run_web_research',
        queue='part_parsing',
        args=[run.pk],
        status=BackgroundJobDispatch.Status.RUNNING,
        claim_token='1a13ba4d-32b5-40d0-92ca-a4401016b6c5',
        run_attempts=1,
        max_run_attempts=3,
    )
    lock = MagicMock()
    lock.acquire.return_value = False

    class FakeRedisCache:
        def lock(self, *_args, **_kwargs):
            return lock

    with patch('apps.web_research.tasks.RedisCache', FakeRedisCache), patch(
        'apps.web_research.tasks.cache', FakeRedisCache(),
    ):
        result = execute_claimed_dispatch(dispatch)

    dispatch.refresh_from_db()
    run.refresh_from_db()
    assert result['status'] == 'retrying'
    assert dispatch.status == BackgroundJobDispatch.Status.PENDING
    assert dispatch.finished_at is None
    assert run.status == WebResearchRun.Status.QUEUED


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('workflow_status', 'attempt_apply_state'),
    [
        ('apply_pending', 'pending'),
        ('applied', 'applied'),
    ],
)
def test_exhausted_web_dispatch_does_not_strand_replayable_local_work(
    workflow_status,
    attempt_apply_state,
):
    from apps.products.models import Product
    from apps.tenants.services import TenantService
    from apps.web_research.models import (
        WebResearchRun,
        WebSearchAttempt,
        WebSearchWorkflow,
    )

    tenant, _ = TenantService.create_tenant(
        'Web checkpoint dispatch exhaustion',
        'web-checkpoint-dispatch-exhaustion',
        'web-checkpoint-dispatch-exhaustion@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='WEB-CHECKPOINT-EXHAUSTION',
        name='Web checkpoint exhaustion',
        price='1.00',
    )
    run = WebResearchRun.objects.create(
        tenant=tenant,
        product=product,
        status=WebResearchRun.Status.RUNNING,
    )
    workflow = WebSearchWorkflow.objects.create(
        tenant=tenant,
        product=product,
        run=run,
        operation='web_research',
        domain_reference=f'product:{product.pk}:purpose:enrichment',
        workflow_key=f'web-research-run:{run.pk}',
        input_fingerprint='a' * 64,
        input_snapshot={'version': 1},
        status=workflow_status,
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
        request_fingerprint='b' * 64,
        query='durable checkpoint',
        status=WebSearchAttempt.Status.SUCCESS,
        checkpoint_enc=b'encrypted-checkpoint-placeholder',
        reconciliation_state=WebSearchAttempt.ReconciliationState.NOT_REQUIRED,
        apply_state=attempt_apply_state,
    )
    dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.web_research.tasks.run_web_research',
        queue='part_parsing',
        args=[run.pk],
        deduplication_key=f'web-research-run:{run.pk}',
        status=BackgroundJobDispatch.Status.RUNNING,
        claim_token='1a13ba4d-32b5-40d0-92ca-a4401016b6c5',
        run_attempts=3,
        max_run_attempts=3,
    )
    target = SimpleNamespace(
        run=lambda _run_id: (_ for _ in ()).throw(
            RuntimeError('local apply repeatedly unavailable'),
        ),
    )

    with patch('apps.core.dispatch._registered_task', return_value=target):
        result = execute_claimed_dispatch(dispatch)

    dispatch.refresh_from_db()
    run.refresh_from_db()
    workflow.refresh_from_db()
    assert result['status'] == 'failed'
    assert dispatch.status == BackgroundJobDispatch.Status.FAILED
    assert run.status == WebResearchRun.Status.QUEUED
    assert run.finished_at is None
    assert workflow.status == workflow_status


@pytest.mark.django_db
def test_bulk_retry_resets_running_domain_job_without_losing_batch():
    from apps.products.models import ProductBulkActionJob
    from apps.tenants.services import TenantService

    tenant, _ = TenantService.create_tenant(
        'Durable bulk retry',
        'durable-bulk-retry',
        'durable-bulk-retry@example.com',
        'pass12345',
    )
    bulk_job = ProductBulkActionJob.objects.create(
        tenant=tenant,
        action=ProductBulkActionJob.Action.FIND_IMAGES,
        status=ProductBulkActionJob.Status.RUNNING,
        product_ids=[101],
        total_count=1,
    )
    dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.products.tasks.process_bulk_product_action',
        queue='part_parsing_bulk',
        args=[bulk_job.pk],
        status=BackgroundJobDispatch.Status.RUNNING,
        claim_token='1a13ba4d-32b5-40d0-92ca-a4401016b6c5',
        run_attempts=1,
    )
    target = SimpleNamespace(run=lambda job_id: (_ for _ in ()).throw(RuntimeError('boom')))

    with patch('apps.core.dispatch._registered_task', return_value=target):
        result = execute_claimed_dispatch(dispatch)

    bulk_job.refresh_from_db()
    dispatch.refresh_from_db()
    assert result['status'] == 'retrying'
    assert dispatch.status == BackgroundJobDispatch.Status.PENDING
    assert bulk_job.status == ProductBulkActionJob.Status.PENDING
    assert bulk_job.processed_count == 0


@pytest.mark.django_db
def test_exhausted_bulk_dispatch_marks_domain_job_failed():
    from apps.products.models import ProductBulkActionJob
    from apps.tenants.services import TenantService

    tenant, _ = TenantService.create_tenant(
        'Durable bulk terminal',
        'durable-bulk-terminal',
        'durable-bulk-terminal@example.com',
        'pass12345',
    )
    bulk_job = ProductBulkActionJob.objects.create(
        tenant=tenant,
        action=ProductBulkActionJob.Action.FIND_IMAGES,
        status=ProductBulkActionJob.Status.RUNNING,
        product_ids=[101],
        total_count=1,
    )
    dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.products.tasks.process_bulk_product_action',
        queue='part_parsing_bulk',
        args=[bulk_job.pk],
        status=BackgroundJobDispatch.Status.RUNNING,
        claim_token='1a13ba4d-32b5-40d0-92ca-a4401016b6c5',
        run_attempts=4,
        max_run_attempts=4,
    )
    target = SimpleNamespace(
        run=lambda job_id: (_ for _ in ()).throw(RuntimeError('terminal boom')),
    )

    with patch('apps.core.dispatch._registered_task', return_value=target):
        result = execute_claimed_dispatch(dispatch)

    bulk_job.refresh_from_db()
    dispatch.refresh_from_db()
    assert result['status'] == 'failed'
    assert dispatch.status == BackgroundJobDispatch.Status.FAILED
    assert bulk_job.status == ProductBulkActionJob.Status.FAILED
    assert bulk_job.finished_at is not None
