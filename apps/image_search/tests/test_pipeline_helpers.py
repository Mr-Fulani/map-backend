from importlib import import_module
import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import uuid

import pytest
import requests

from apps.core.models import BackgroundJobDispatch
from apps.core.paid_search_recovery import resume_image_search_checkpoint
from apps.image_search.models import (
    ImageSearchIntent,
    ImageSearchLog,
    ImageSearchTask,
)
from apps.image_search.services.pipeline import _final_outcome, build_cache_key, run_for_product
from apps.image_search.sources.base import ImageCandidate, ImageSearchOutcomeUncertain
from apps.image_search.sources.brave import BraveImageSource
from apps.image_search.sources.tavily import TavilyImageSource
from apps.products.models import Product, ProductImage
from apps.tenants.services import TenantService
from apps.web_research.accounting import resolve_web_search_attempt
from apps.web_research.models import (
    WebSearchAttempt,
    WebSearchConnection,
    WebSearchWorkflow,
)


class ProductStub:
    pk = 10
    tenant_id = 20
    article = ''
    brand = ''
    catalog_category_id = None


def _fake_source_plan(product, source, queries):
    plan = {
        'source_id': source.source_id,
        'source_index': 0,
        'is_free': False,
        'connection_id': None,
        'queries': [
            {'query': query, 'confidence': confidence}
            for query, confidence in queries
        ],
    }
    source.workflow_plan = plan
    source.planned_queries.return_value = queries
    return {
        'version': 1,
        'kind': 'image_search',
        'product_id': product.pk,
        'tenant_id': product.tenant_id,
        'sources': [plan],
    }


def _managed_search_connection(provider_id: str) -> WebSearchConnection:
    connection = WebSearchConnection.objects.create(
        provider_id=provider_id,
        display_name=f'{provider_id} image workflow test',
        is_active=True,
        priority=10,
        requests_per_minute=100,
        monthly_request_limit=1000,
    )
    connection.set_credentials({'api_key': 'test-key'})
    connection.save(update_fields=['credentials_enc', 'updated_at'])
    return connection


def _tracking(product, suffix: str) -> ImageSearchTask:
    return ImageSearchTask.objects.create(
        tenant=product.tenant,
        product=product,
        task_id=f'image-workflow-{suffix}',
    )


def _brave_provider_result(url: str) -> dict:
    return {
        'title': f'{url} BREMBO P50136',
        'properties': {'url': url, 'width': 1200, 'height': 900},
        'thumbnail': {'width': 200, 'height': 150},
    }


def test_cache_key_is_tenant_and_product_scoped_for_blank_identity():
    first = ProductStub()
    second = ProductStub()
    second.pk = 11
    assert build_cache_key(first) != build_cache_key(second)


def test_cache_key_changes_with_tenant():
    first = ProductStub()
    second = ProductStub()
    second.tenant_id = 21
    assert build_cache_key(first) != build_cache_key(second)


def test_outcome_explains_relevance_rejections_instead_of_rate_limit():
    result = _final_outcome(
        saved=[],
        found_count=30,
        rejected_count=30,
        eligible_count=0,
        download_failed_count=0,
        attempted_sources=['brave', 'tavily'],
        errors=[],
    )

    assert result['reason_code'] == 'rejected_by_relevance'
    assert '30' in result['message']
    assert 'огранич' not in result['message'].lower()


def test_outcome_reports_low_quality_images_as_processing_candidates():
    result = _final_outcome(
        saved=[SimpleNamespace(pk=1, status=ProductImage.Status.LOW_CONFIDENCE)],
        found_count=1,
        rejected_count=0,
        eligible_count=1,
        download_failed_count=0,
        attempted_sources=['brave'],
        errors=[],
    )

    assert result['reason_code'] == 'found'
    assert result['saved_count'] == 1
    assert 'улучшения качества: 1' in result['message']


@pytest.mark.django_db
def test_web_search_creates_candidates_with_review_status():
    tenant, _ = TenantService.create_tenant(
        'Image Review', 'image-review-status', 'image-review@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='P50136',
        brand='BREMBO',
        name='Колодки тормозные BREMBO P50136',
        price='1000.00',
    )
    query = 'BREMBO "P50136" автозапчасть'
    candidate = ImageCandidate(
        url='https://images.example.com/brembo-P50136.jpg',
        source_id='brave',
        tier=3,
        width=1200,
        height=900,
        raw_meta={'query': query, 'confidence': 'HIGH', 'title': 'BREMBO P50136'},
    )
    source = MagicMock(
        source_id='brave',
        max_queries=1,
        last_error='',
        last_error_code='',
    )
    snapshot = _fake_source_plan(product, source, [(query, 'HIGH')])
    source.search.return_value = [candidate]
    review_image = ProductImage.objects.create(
        product=product,
        s3_key='products/test/brembo-P50136.jpg',
        s3_key_thumb='products/test/brembo-P50136_thumb.jpg',
        url_source=candidate.url,
        sha256='a' * 64,
        status=ProductImage.Status.NEEDS_REVIEW,
    )
    uploader = MagicMock()
    uploader.process.return_value = review_image

    with patch(
        'apps.image_search.services.pipeline.build_image_search_workflow_snapshot',
        return_value=snapshot,
    ), patch(
        'apps.image_search.services.pipeline.get_workflow_sources',
        return_value=[source],
    ), patch(
        'apps.image_search.services.pipeline.PhotoUploadPipeline', return_value=uploader,
    ), patch(
        'apps.image_search.services.pipeline.candidate_metadata_assessment',
        return_value=(True, ['trusted_code_match'], 0.95),
    ), patch(
        'apps.image_search.services.pipeline.score', return_value=0.82,
    ), patch('apps.image_search.services.pipeline._record_candidate_assessments'):
        result = run_for_product(
            product,
            workflow_key='image-search-task:review-status',
        )

    assert result['saved_count'] == 1
    uploader.process.assert_called_once_with(
        candidate.url,
        product,
        source_id='brave',
        status=ProductImage.Status.NEEDS_REVIEW,
        validate_quality=True,
        allow_low_resolution=True,
    )


@pytest.mark.django_db
def test_uncertain_paid_source_is_persisted_and_stops_provider_fallback():
    tenant, _ = TenantService.create_tenant(
        'Image uncertain', 'image-provider-uncertain',
        'image-provider-uncertain@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='P50136',
        brand='BREMBO',
        name='BREMBO P50136',
        price='1000.00',
    )
    query = 'BREMBO P50136'
    first = MagicMock(
        source_id='brave',
        max_queries=2,
        last_attempt_query=query,
        last_error='Результат Brave Image Search неизвестен; повтор запрещён.',
        last_error_code='outcome_uncertain',
    )
    snapshot = _fake_source_plan(
        product,
        first,
        [(query, 'HIGH'), ('BREMBO brake pads', 'MEDIUM')],
    )
    first.search.side_effect = ImageSearchOutcomeUncertain(first.last_error)
    second = MagicMock(source_id='tavily', max_queries=2)

    with patch(
        'apps.image_search.services.pipeline.build_image_search_workflow_snapshot',
        return_value=snapshot,
    ), patch(
        'apps.image_search.services.pipeline.get_workflow_sources',
        return_value=[first, second],
    ), pytest.raises(ImageSearchOutcomeUncertain):
        run_for_product(
            product,
            workflow_key='image-search-task:uncertain-source',
        )

    second.search.assert_not_called()
    attempt = ImageSearchLog.objects.get(product=product, source_id='brave')
    assert attempt.query == query
    assert attempt.outcome == ImageSearchLog.Outcome.OUTCOME_UNCERTAIN
    assert attempt.error_code == 'outcome_uncertain'
    assert 'api.search.brave.com' not in attempt.error


@pytest.mark.django_db
def test_new_intent_is_fenced_from_paid_image_call_until_manual_resolution(
    settings,
):
    tenant, _ = TenantService.create_tenant(
        'Image domain fence',
        'image-domain-fence',
        'image-domain-fence@test.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='P50136',
        brand='BREMBO',
        name='BREMBO P50136',
        price='1000.00',
    )
    intent_keys = (uuid.uuid4(), uuid.uuid4())
    for key in intent_keys:
        ImageSearchIntent.objects.create(
            tenant=tenant,
            operation=ImageSearchIntent.Operation.SINGLE,
            idempotency_key=key,
            request_fingerprint=str(key).replace('-', ''),
            request_payload={'product_id': product.pk},
        )
    connection = WebSearchConnection.objects.create(
        provider_id='brave',
        display_name='Brave image test',
        is_active=True,
        priority=10,
        requests_per_minute=20,
        monthly_request_limit=100,
    )
    connection.set_credentials({'api_key': 'test-key'})
    connection.save(update_fields=['credentials_enc', 'updated_at'])
    fallback_connection = WebSearchConnection.objects.create(
        provider_id='tavily',
        display_name='Tavily image test',
        is_active=True,
        priority=20,
        requests_per_minute=20,
        monthly_request_limit=100,
    )
    fallback_connection.set_credentials({'api_key': 'test-key'})
    fallback_connection.save(update_fields=['credentials_enc', 'updated_at'])
    settings.IMAGE_SOURCES_ENABLED = ['brave']

    with patch.object(BraveImageSource, 'max_queries', 1), patch(
        'apps.image_search.sources.brave.bounded_http_request',
        side_effect=requests.ReadTimeout('provider outcome unknown'),
    ) as first_network, pytest.raises(ImageSearchOutcomeUncertain):
        run_for_product(
            product,
            workflow_key='image-search-task:first-intent',
        )

    first_network.assert_called_once()
    attempt = WebSearchAttempt.objects.get(
        tenant=tenant,
        operation='image_search',
        domain_reference=f'product:{tenant.pk}:{product.pk}',
    )
    assert attempt.status == WebSearchAttempt.Status.OUTCOME_UNCERTAIN
    assert attempt.reconciliation_state == WebSearchAttempt.ReconciliationState.PENDING

    blocked_network = MagicMock()
    settings.IMAGE_SOURCES_ENABLED = ['tavily']
    with patch.object(TavilyImageSource, 'max_queries', 1), patch(
        'apps.image_search.sources.tavily.bounded_http_request',
        blocked_network,
    ), pytest.raises(
        ImageSearchOutcomeUncertain,
        match='требует сверки',
    ) as blocked:
        run_for_product(
            product,
            workflow_key='image-search-task:second-intent',
        )

    assert blocked.value.code == 'provider_reconciliation_required'
    blocked_network.assert_not_called()
    assert ImageSearchIntent.objects.filter(
        tenant=tenant,
        idempotency_key__in=intent_keys,
    ).count() == 2
    assert WebSearchAttempt.objects.filter(
        tenant=tenant,
        operation='image_search',
    ).count() == 1

    resolve_web_search_attempt(
        attempt.pk,
        action='accepted',
        operator_note='provider dashboard confirmed the first request',
    )
    rejected_response = MagicMock(status_code=401)
    settings.IMAGE_SOURCES_ENABLED = ['brave']
    with patch.object(BraveImageSource, 'max_queries', 1), patch(
        'apps.image_search.sources.brave.bounded_http_request',
        return_value=rejected_response,
    ) as resumed_network:
        result = run_for_product(
            product,
            workflow_key='image-search-task:third-intent',
        )

    resumed_network.assert_called_once()
    assert result['saved_count'] == 0
    assert WebSearchAttempt.objects.filter(
        tenant=tenant,
        operation='image_search',
    ).count() == 2


@pytest.mark.django_db
def test_image_workflow_crash_before_checkpoint_never_repeats_provider(
    settings,
):
    tenant, _ = TenantService.create_tenant(
        'Image pre-checkpoint crash',
        'image-pre-checkpoint-crash',
        'image-pre-checkpoint-crash@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='P50136',
        brand='BREMBO',
        name='BREMBO P50136',
        price='1000.00',
    )
    tracking = _tracking(product, 'pre-checkpoint-crash')
    _managed_search_connection('brave')
    settings.IMAGE_SOURCES_ENABLED = ['brave']

    with patch.object(BraveImageSource, 'max_queries', 1), patch.object(
        BraveImageSource,
        '_fetch_runtime',
        side_effect=KeyboardInterrupt('worker killed after reservation'),
    ) as first_provider, pytest.raises(KeyboardInterrupt):
        run_for_product(
            product,
            workflow_key=f'image-search-task:{tracking.pk}',
            tracking_id=tracking.pk,
        )

    first_provider.assert_called_once()
    workflow = WebSearchWorkflow.objects.get(
        workflow_key=f'image-search-task:{tracking.pk}',
    )
    attempt = workflow.attempts.get()
    assert workflow.status == WebSearchWorkflow.Status.IN_PROGRESS
    assert attempt.status == WebSearchAttempt.Status.STARTED
    assert (
        attempt.reconciliation_state
        == WebSearchAttempt.ReconciliationState.PENDING
    )

    with patch.object(BraveImageSource, '_fetch_runtime') as repeated_provider, \
         pytest.raises(ImageSearchOutcomeUncertain):
        run_for_product(
            product,
            workflow_key=f'image-search-task:{tracking.pk}',
            tracking_id=tracking.pk,
        )

    repeated_provider.assert_not_called()
    assert workflow.attempts.count() == 1


@pytest.mark.django_db
def test_image_workflow_does_not_substitute_recreated_connection_before_send(
    settings,
):
    tenant, _ = TenantService.create_tenant(
        'Image immutable connection',
        'image-immutable-connection',
        'image-immutable-connection@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='P50136',
        brand='BREMBO',
        name='BREMBO P50136',
        price='1000.00',
    )
    tracking = _tracking(product, 'immutable-connection')
    original = _managed_search_connection('brave')
    original_id = original.pk
    settings.IMAGE_SOURCES_ENABLED = ['brave']

    # Stop after the immutable plan is committed but before any call slot is
    # reserved. This models a worker loss in the pre-provider window.
    with patch(
        'apps.image_search.services.pipeline.get_workflow_sources',
        side_effect=RuntimeError('worker killed after workflow acquire'),
    ), pytest.raises(RuntimeError, match='after workflow acquire'):
        run_for_product(
            product,
            workflow_key=f'image-search-task:{tracking.pk}',
            tracking_id=tracking.pk,
        )

    workflow = WebSearchWorkflow.objects.get(
        workflow_key=f'image-search-task:{tracking.pk}',
    )
    assert workflow.input_snapshot['sources'][0]['connection_id'] == original_id
    assert not workflow.attempts.exists()

    original.delete()
    replacement = _managed_search_connection('brave')
    assert replacement.pk != original_id

    with patch.object(BraveImageSource, '_fetch_runtime') as paid_provider:
        result = run_for_product(
            product,
            workflow_key=f'image-search-task:{tracking.pk}',
            tracking_id=tracking.pk,
        )

    paid_provider.assert_not_called()
    workflow.refresh_from_db()
    assert workflow.status == WebSearchWorkflow.Status.APPLIED
    assert result['reason_code'] == 'source_error'
    assert result['errors'][0]['code'] == 'provider_connection_changed'


@pytest.mark.django_db
def test_image_workflow_replays_checkpoint_after_local_apply_crash(
    settings,
):
    tenant, _ = TenantService.create_tenant(
        'Image checkpoint replay',
        'image-checkpoint-replay',
        'image-checkpoint-replay@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='P50136',
        brand='BREMBO',
        name='BREMBO P50136',
        price='1000.00',
    )
    tracking = _tracking(product, 'checkpoint-replay')
    _managed_search_connection('brave')
    settings.IMAGE_SOURCES_ENABLED = ['brave']
    image_url = 'https://images.example.com/checkpoint.jpg'

    with patch.object(BraveImageSource, 'max_queries', 1), patch.object(
        BraveImageSource,
        '_fetch_runtime',
        return_value=[_brave_provider_result(image_url)],
    ) as paid_provider, patch(
        'apps.image_search.services.pipeline.candidate_metadata_assessment',
        side_effect=RuntimeError('worker killed before domain apply'),
    ), pytest.raises(RuntimeError, match='before domain apply'):
        run_for_product(
            product,
            workflow_key=f'image-search-task:{tracking.pk}',
            tracking_id=tracking.pk,
        )

    paid_provider.assert_called_once()
    workflow = WebSearchWorkflow.objects.get(
        workflow_key=f'image-search-task:{tracking.pk}',
    )
    attempt = workflow.attempts.get()
    assert workflow.status == WebSearchWorkflow.Status.APPLY_PENDING
    assert attempt.status == WebSearchAttempt.Status.SUCCESS
    assert attempt.apply_state == WebSearchAttempt.ApplyState.PENDING

    image = ProductImage.objects.create(
        product=product,
        s3_key='products/tests/checkpoint.jpg',
        sha256='c' * 64,
        url_source=image_url,
        status=ProductImage.Status.NEEDS_REVIEW,
    )
    uploader = MagicMock()
    uploader.process.return_value = image
    with patch.object(BraveImageSource, '_fetch_runtime') as repeated_provider, patch(
        'apps.image_search.services.pipeline.PhotoUploadPipeline',
        return_value=uploader,
    ), patch(
        'apps.image_search.services.pipeline.candidate_metadata_assessment',
        return_value=(True, ['trusted_code_match'], 0.99),
    ), patch(
        'apps.image_search.services.pipeline.score',
        return_value=0.90,
    ), patch(
        'apps.image_search.services.pipeline._record_candidate_assessments',
    ):
        result = run_for_product(
            product,
            workflow_key=f'image-search-task:{tracking.pk}',
            tracking_id=tracking.pk,
        )

    repeated_provider.assert_not_called()
    assert result['saved_count'] == 1
    workflow.refresh_from_db()
    attempt.refresh_from_db()
    tracking.refresh_from_db()
    assert workflow.status == WebSearchWorkflow.Status.APPLIED
    assert attempt.apply_state == WebSearchAttempt.ApplyState.APPLIED
    assert tracking.status == ImageSearchTask.Status.SUCCEEDED
    assert tracking.result['product_image_ids'] == [image.pk]


@pytest.mark.django_db
def test_image_multi_query_attempts_share_one_workflow_and_exact_ack(settings):
    tenant, _ = TenantService.create_tenant(
        'Image multi query',
        'image-multi-query',
        'image-multi-query@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='P50136',
        brand='BREMBO',
        name='BREMBO P50136',
        price='1000.00',
    )
    tracking = _tracking(product, 'multi-query')
    _managed_search_connection('brave')
    settings.IMAGE_SOURCES_ENABLED = ['brave']

    planned_queries = [
        ('BREMBO "P50136" first image', 'HIGH'),
        ('BREMBO "P50136" second image', 'MEDIUM'),
    ]
    with patch.object(
        BraveImageSource,
        'build_queries',
        return_value=planned_queries,
    ), patch.object(BraveImageSource, 'max_queries', 2), patch.object(
        BraveImageSource,
        '_fetch_runtime',
        side_effect=[
            [_brave_provider_result('https://images.example.com/one.jpg')],
            [_brave_provider_result('https://images.example.com/two.jpg')],
        ],
    ) as paid_provider, patch(
        'apps.image_search.services.pipeline.candidate_metadata_assessment',
        return_value=(False, ['identity_mismatch'], 0.10),
    ), patch(
        'apps.image_search.services.pipeline._record_candidate_assessments',
    ):
        result = run_for_product(
            product,
            workflow_key=f'image-search-task:{tracking.pk}',
            tracking_id=tracking.pk,
        )

    assert paid_provider.call_count == 2
    workflow = WebSearchWorkflow.objects.get(
        workflow_key=f'image-search-task:{tracking.pk}',
    )
    attempts = list(workflow.attempts.order_by('call_key'))
    assert len(attempts) == 2
    assert {attempt.apply_state for attempt in attempts} == {
        WebSearchAttempt.ApplyState.APPLIED,
    }
    assert workflow.status == WebSearchWorkflow.Status.APPLIED
    assert result['found_count'] == 2


@pytest.mark.django_db
def test_image_partial_local_apply_resumes_without_provider_or_duplicates(
    settings,
):
    tenant, _ = TenantService.create_tenant(
        'Image partial local apply',
        'image-partial-local-apply',
        'image-partial-local-apply@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='P50136',
        brand='BREMBO',
        name='BREMBO P50136',
        price='1000.00',
    )
    tracking = _tracking(product, 'partial-local-apply')
    _managed_search_connection('brave')
    settings.IMAGE_SOURCES_ENABLED = ['brave']
    urls = [
        'https://images.example.com/partial-one.jpg',
        'https://images.example.com/partial-two.jpg',
    ]
    uploader = MagicMock()

    def save_or_restore(url, product, **kwargs):
        digest = hashlib.sha256(url.encode()).hexdigest()
        image, _ = ProductImage.objects.get_or_create(
            product=product,
            url_source=url,
            defaults={
                's3_key': f'products/tests/{digest}.jpg',
                'sha256': digest,
                'status': ProductImage.Status.NEEDS_REVIEW,
            },
        )
        return image

    uploader.process.side_effect = save_or_restore
    failed_once = False

    def crash_after_first_saved(_product, candidates, *, verdict, **kwargs):
        nonlocal failed_once
        if verdict == 'review' and candidates and not failed_once:
            failed_once = True
            raise RuntimeError('worker killed after first saved image')

    with patch.object(BraveImageSource, 'max_queries', 1), patch.object(
        BraveImageSource,
        '_fetch_runtime',
        return_value=[_brave_provider_result(url) for url in urls],
    ) as paid_provider, patch(
        'apps.image_search.services.pipeline.PhotoUploadPipeline',
        return_value=uploader,
    ), patch(
        'apps.image_search.services.pipeline.candidate_metadata_assessment',
        return_value=(True, ['trusted_code_match'], 0.99),
    ), patch(
        'apps.image_search.services.pipeline.score',
        return_value=0.90,
    ), patch(
        'apps.image_search.services.pipeline._record_candidate_assessments',
        side_effect=crash_after_first_saved,
    ), pytest.raises(RuntimeError, match='after first saved image'):
        run_for_product(
            product,
            workflow_key=f'image-search-task:{tracking.pk}',
            tracking_id=tracking.pk,
        )

    paid_provider.assert_called_once()
    assert ProductImage.objects.filter(product=product).count() == 1

    with patch.object(BraveImageSource, '_fetch_runtime') as repeated_provider, patch(
        'apps.image_search.services.pipeline.PhotoUploadPipeline',
        return_value=uploader,
    ), patch(
        'apps.image_search.services.pipeline.candidate_metadata_assessment',
        return_value=(True, ['trusted_code_match'], 0.99),
    ), patch(
        'apps.image_search.services.pipeline.score',
        return_value=0.90,
    ), patch(
        'apps.image_search.services.pipeline._record_candidate_assessments',
    ):
        result = run_for_product(
            product,
            workflow_key=f'image-search-task:{tracking.pk}',
            tracking_id=tracking.pk,
        )

    repeated_provider.assert_not_called()
    assert result['saved_count'] == 2
    assert ProductImage.objects.filter(product=product).count() == 2
    assert set(ProductImage.objects.filter(product=product).values_list(
        'url_source', flat=True,
    )) == set(urls)


@pytest.mark.django_db
def test_image_full_slots_after_partial_apply_ack_without_new_provider(settings):
    tenant, _ = TenantService.create_tenant(
        'Image full after partial apply',
        'image-full-after-partial',
        'image-full-after-partial@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='P50136',
        brand='BREMBO',
        name='BREMBO P50136',
        price='1000.00',
    )
    tracking = _tracking(product, 'full-after-partial')
    dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.image_search.tasks.search_images_for_product',
        queue='image_search',
        args=[product.pk, tracking.pk],
        deduplication_key=f'image-search-request:{tracking.task_id}',
        max_run_attempts=5,
    )
    tracking.dispatch = dispatch
    tracking.save(update_fields=['dispatch', 'updated_at'])
    _managed_search_connection('brave')
    settings.IMAGE_SOURCES_ENABLED = ['brave']
    settings.IMAGE_SEARCH_SETTINGS = {
        **settings.IMAGE_SEARCH_SETTINGS,
        'MAX_IMAGES_PER_PRODUCT': 1,
    }
    image_url = 'https://images.example.com/full-after-partial.jpg'
    uploader = MagicMock()

    def save_first_image(url, owner, **kwargs):
        return ProductImage.objects.create(
            product=owner,
            s3_key='products/tests/full-after-partial.jpg',
            sha256='f' * 64,
            url_source=url,
            status=ProductImage.Status.NEEDS_REVIEW,
        )

    def crash_after_full_slot(_product, candidates, *, verdict, **kwargs):
        if verdict == 'review' and candidates:
            raise RuntimeError('worker killed after full-slot save')

    uploader.process.side_effect = save_first_image

    with patch.object(BraveImageSource, 'max_queries', 1), patch.object(
        BraveImageSource,
        '_fetch_runtime',
        return_value=[_brave_provider_result(image_url)],
    ) as paid_provider, patch(
        'apps.image_search.services.pipeline.PhotoUploadPipeline',
        return_value=uploader,
    ), patch(
        'apps.image_search.services.pipeline.candidate_metadata_assessment',
        return_value=(True, ['trusted_code_match'], 0.99),
    ), patch(
        'apps.image_search.services.pipeline.score',
        return_value=0.90,
    ), patch(
        'apps.image_search.services.pipeline._record_candidate_assessments',
        side_effect=crash_after_full_slot,
    ), pytest.raises(RuntimeError, match='full-slot save'):
        run_for_product(
            product,
            workflow_key=f'image-search-task:{tracking.pk}',
            tracking_id=tracking.pk,
        )

    paid_provider.assert_called_once()
    image = ProductImage.objects.get(product=product)
    workflow = WebSearchWorkflow.objects.get(
        workflow_key=f'image-search-task:{tracking.pk}',
    )
    assert workflow.status == WebSearchWorkflow.Status.APPLY_PENDING

    # Exhaustion after repeated local apply failures must not permit a new
    # business intent to bypass the active paid-result fence.
    BackgroundJobDispatch.objects.filter(pk=dispatch.pk).update(
        status=BackgroundJobDispatch.Status.FAILED,
        run_attempts=dispatch.max_run_attempts,
    )
    ImageSearchTask.objects.filter(pk=tracking.pk).update(
        status=ImageSearchTask.Status.FAILED,
    )
    with patch.object(BraveImageSource, '_fetch_runtime') as new_intent_provider, \
         pytest.raises(ImageSearchOutcomeUncertain):
        run_for_product(
            product,
            workflow_key='image-search-task:distinct-blocked-intent',
        )
    new_intent_provider.assert_not_called()

    recovered = resume_image_search_checkpoint(tracking.pk)
    assert recovered.pk == dispatch.pk
    assert recovered.status == BackgroundJobDispatch.Status.PENDING
    tracking.refresh_from_db()
    assert tracking.status == ImageSearchTask.Status.PENDING

    with patch.object(BraveImageSource, '_fetch_runtime') as repeated_provider:
        outcome = run_for_product(
            product,
            workflow_key=f'image-search-task:{tracking.pk}',
            tracking_id=tracking.pk,
        )

    repeated_provider.assert_not_called()
    workflow.refresh_from_db()
    tracking.refresh_from_db()
    assert outcome['reason_code'] == 'already_has_images'
    assert outcome['product_image_ids'] == [image.pk]
    assert workflow.status == WebSearchWorkflow.Status.APPLIED
    assert tracking.status == ImageSearchTask.Status.SUCCEEDED


@pytest.mark.django_db
def test_image_task_after_ack_is_a_zero_provider_noop():
    from apps.image_search.tasks import search_images_for_product

    tenant, _ = TenantService.create_tenant(
        'Image applied no-op',
        'image-applied-noop',
        'image-applied-noop@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='NOOP-1',
        name='Applied image result',
        price='1.00',
    )
    tracking = _tracking(product, 'applied-noop')
    expected = {'saved_count': 1, 'reason_code': 'found'}
    ImageSearchTask.objects.filter(pk=tracking.pk).update(
        status=ImageSearchTask.Status.SUCCEEDED,
        result=expected,
    )

    class FakeLock:
        def acquire(self, blocking=False):
            return True

        def release(self):
            return None

    class FakeRedisCache:
        def lock(self, key, timeout):
            return FakeLock()

    fake_cache = FakeRedisCache()
    with patch('apps.image_search.tasks.cache', fake_cache), patch(
        'apps.image_search.tasks.RedisCache',
        FakeRedisCache,
    ), patch(
        'apps.image_search.services.pipeline.run_for_product',
    ) as pipeline:
        result = search_images_for_product.run(product.pk, tracking.pk)

    assert result == expected
    pipeline.assert_not_called()


@pytest.mark.django_db
def test_web_search_imported_backfill_only_restores_search_sources():
    tenant, _ = TenantService.create_tenant(
        'Image Backfill', 'image-backfill-status', 'image-backfill@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='P50136',
        name='BREMBO P50136',
        price='1000.00',
    )
    brave_image = ProductImage.objects.create(
        product=product,
        s3_key='products/test/brave.jpg',
        sha256='b' * 64,
        source_id='brave',
        status=ProductImage.Status.IMPORTED,
    )
    imported_image = ProductImage.objects.create(
        product=product,
        s3_key='products/test/1c.jpg',
        sha256='c' * 64,
        source_id='1c',
        status=ProductImage.Status.IMPORTED,
    )

    migration = import_module(
        'apps.products.migrations.0031_restore_web_search_images_for_review',
    )
    from django.apps import apps
    migration.restore_web_search_images_for_review(apps, None)

    brave_image.refresh_from_db()
    imported_image.refresh_from_db()
    assert brave_image.status == ProductImage.Status.NEEDS_REVIEW
    assert imported_image.status == ProductImage.Status.IMPORTED
