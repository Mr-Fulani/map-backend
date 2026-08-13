import json
import io
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connections
from django.test import Client
from django.utils import timezone

from apps.core.models import BackgroundJobDispatch
from apps.products.enrichment import normalize_part_code
from apps.products.models import (
    GlobalPartFitment, Product, ProductCatalogClassification, ProductCrossCode, ProductEnrichmentFact,
    ProductParseIntent, ProductParseJob, ReviewStatus, VehicleFitment,
    TenantCatalogCategory, TenantCategoryMapping,
)
from apps.products.services import ProductEnrichmentService, ProductKnowledgeGraphService
from apps.products.views import _serialize_review_item
from apps.tenants.models import CatalogDomain
from apps.tenants.tests.auth import create_tenant_with_operator_key, owner_client


def make_tenant(slug, catalog_domain='auto_parts'):
    tenant, api_key = create_tenant_with_operator_key(
        slug, slug, f'{slug}@test.com', 'pass12345',
    )
    tenant.catalog_domain = catalog_domain
    tenant.save(update_fields=['catalog_domain'])
    from apps.products.services import ProductCategorySeedService
    ProductCategorySeedService.enable_tenant_catalog_domain(tenant, catalog_domain)
    return tenant, api_key


def make_product(tenant, article='P50136', brand='BREMBO', name=None, category_1c=''):
    return Product.objects.create(
        tenant=tenant,
        article=article,
        brand=brand,
        name=name or f'{brand} {article}',
        category_1c=category_1c,
        price=Decimal('0'),
        stock_qty=0,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('method', 'path'),
    [
        ('post', '/api/v1/products/catalog-categories/assign/'),
        ('post', '/api/v1/products/exclude/'),
        ('delete', '/api/v1/products/bulk-delete/'),
    ],
)
def test_direct_product_bulk_endpoints_enforce_hard_cap(method, path, settings):
    tenant, api_key = make_tenant(f'direct-cap-{method}-{path.split("/")[-2]}')
    settings.API_BULK_MAX_ITEMS = 500
    client = Client()
    payload = {'product_ids': list(range(1, 502))}
    request = getattr(client, method)

    response = request(
        path,
        payload,
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 400
    assert 'product_ids' in response.json()['errors']


@pytest.mark.django_db
def test_approved_web_brand_fact_updates_product_identity():
    tenant, _ = make_tenant('approve-web-brand')
    product = make_product(tenant, brand='', name='Фонарь Kia Optima')
    fact = ProductEnrichmentFact.objects.create(
        tenant=tenant,
        product=product,
        source_id='web_research',
        fact_type=ProductEnrichmentFact.FactType.BRAND,
        name='Предполагаемый бренд',
        value='OEM',
        needs_review=True,
    )
    client = owner_client(tenant)

    response = client.post(
        f'/api/v1/products/{product.pk}/enrichment-facts/{fact.pk}/approve/',
    )

    assert response.status_code == 200
    product.refresh_from_db()
    assert product.brand == 'OEM'
    assert product.brand_resolution_status == Product.BrandResolutionStatus.MANUAL
    assert product.brand_source_id == 'human_review'


@pytest.mark.django_db
def test_approved_web_oem_fact_creates_trusted_cross_code():
    tenant, _ = make_tenant('approve-web-oem')
    product = make_product(tenant, article='P50136', brand='BREMBO')
    fact = ProductEnrichmentFact.objects.create(
        tenant=tenant,
        product=product,
        source_id='web_research',
        fact_type=ProductEnrichmentFact.FactType.OEM,
        name='KIA',
        value='92402D4000',
        raw_text=json.dumps({
            'claim_payload': {
                'manufacturer': 'KIA',
                'code': '92402D4000',
                'code_type': 'OEM',
            },
        }),
        needs_review=True,
    )
    client = owner_client(tenant)

    response = client.post(
        f'/api/v1/products/{product.pk}/enrichment-facts/{fact.pk}/approve/',
    )

    assert response.status_code == 200
    cross = product.cross_codes.get(source_id='human_review')
    product.refresh_from_db()
    assert cross.manufacturer == 'KIA'
    assert cross.normalized_code == '92402D4000'
    assert product.oem_numbers == ['92402D4000']


@pytest.mark.django_db
def test_approved_oem_fact_ignores_malformed_claim_payload():
    tenant, _ = make_tenant('approve-web-oem-malformed')
    product = make_product(tenant, article='P50136', brand='BREMBO')
    fact = ProductEnrichmentFact.objects.create(
        tenant=tenant,
        product=product,
        source_id='web_research',
        fact_type=ProductEnrichmentFact.FactType.OEM,
        name='KIA',
        value='92402D4000',
        raw_text=json.dumps({'claim_payload': ['unexpected', 'list']}),
        review_status=ReviewStatus.APPROVED,
    )

    ProductEnrichmentService.apply_approved_fact(product, fact)

    cross = product.cross_codes.get(source_id='human_review')
    assert cross.manufacturer == 'KIA'
    assert cross.normalized_code == '92402D4000'


@pytest.mark.django_db
def test_parse_endpoint_creates_tenant_scoped_job(django_capture_on_commit_callbacks):
    tenant, api_key = make_tenant('parse-api')
    product = make_product(tenant)
    client = Client()

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                '/api/v1/products/parse/',
                {
                    'product_id': product.pk,
                    'idempotency_key': str(uuid.uuid4()),
                },
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )

    assert response.status_code == 201
    data = response.json()['data']
    job = tenant.product_parse_jobs.get(pk=data['job_id'])
    assert job.product == product
    assert job.normalized_article == normalize_part_code(product.article)
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.products.tasks.parse_single_part',
        args=[job.pk],
    ).exists()
    jobs = tenant.product_parse_jobs.filter(product=product)
    assert set(jobs.values_list('source_id', flat=True)) == {'tachka', 'rossko', 'euroauto'}
    assert set(data['job_ids']) == set(jobs.values_list('pk', flat=True))
    assert BackgroundJobDispatch.objects.count() == 3


@pytest.mark.django_db
def test_parse_endpoint_charges_external_job_budget_once(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('parse-budget-once')
    product = make_product(tenant)
    client = Client()
    payload = {
        'product_id': product.pk,
        'idempotency_key': str(uuid.uuid4()),
    }

    with patch(
        'apps.products.views.consume_transactional_tenant_daily_budget',
    ) as consume, patch(
        'apps.core.tasks.execute_background_dispatch.apply_async',
    ), django_capture_on_commit_callbacks(execute=True):
        first = client.post(
            '/api/v1/products/parse/', payload,
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {api_key}',
        )
        retry = client.post(
            '/api/v1/products/parse/', payload,
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {api_key}',
        )

    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    consume.assert_called_once_with(
        tenant=tenant,
        scope='product-parse-jobs',
        cost=3,
        limit=settings.PRODUCT_PARSE_TENANT_DAILY_JOBS,
    )


@pytest.mark.django_db
def test_parse_budget_exhaustion_rolls_back_intent_jobs_and_dispatches():
    from rest_framework.exceptions import Throttled

    tenant, api_key = make_tenant('parse-budget-exhausted')
    product = make_product(tenant)

    with patch(
        'apps.products.views.consume_transactional_tenant_daily_budget',
        side_effect=Throttled(wait=60),
    ), patch('apps.core.tasks.execute_background_dispatch.apply_async') as publish:
        response = Client().post(
            '/api/v1/products/parse/',
            {
                'product_id': product.pk,
                'idempotency_key': str(uuid.uuid4()),
            },
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {api_key}',
        )

    assert response.status_code == 429
    assert ProductParseIntent.objects.filter(tenant=tenant).count() == 0
    assert ProductParseJob.objects.filter(tenant=tenant).count() == 0
    assert BackgroundJobDispatch.objects.count() == 0
    publish.assert_not_called()


@pytest.mark.django_db
def test_product_detail_exposes_source_offer_and_friendly_price_comparison():
    tenant, api_key = make_tenant('source-offer-api')
    product = make_product(tenant)
    product.price = Decimal('1234.00')
    product.save(update_fields=['price'])
    ProductParseJob.objects.create(
        tenant=tenant,
        product=product,
        brand=product.brand,
        article=product.article,
        normalized_article=normalize_part_code(product.article),
        source_id='euroauto',
        source_url='https://euroauto.ru/part/new/6148741/',
        status=ProductParseJob.Status.SUCCESS,
        source_price=Decimal('1000.00'),
        source_availability=ProductParseJob.SourceAvailability.IN_STOCK,
        source_availability_text='В наличии',
        source_quantity=4,
    )

    response = Client().get(
        f'/api/v1/products/{product.pk}/',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 200
    job = response.json()['data']['parse_jobs_summary'][0]
    assert job['source_label'] == 'Euroauto.ru'
    assert job['source_offer']['price'] == '1000.00'
    assert job['source_offer']['availability_label'] == 'В наличии'
    assert job['source_offer']['quantity'] == 4
    assert job['price_comparison'] == {
        'direction': 'tenant_higher',
        'amount': '234.00',
        'percent': '23.4',
        'tenant_price': '1234.00',
        'source_price': '1000.00',
    }


@pytest.mark.django_db
def test_parse_endpoint_can_generate_after_enrichment(django_capture_on_commit_callbacks):
    tenant, api_key = make_tenant('parse-generate-api')
    product = make_product(tenant)
    client = Client()

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                '/api/v1/products/parse/',
                {
                    'product_id': product.pk,
                    'generate_after': True,
                    'idempotency_key': str(uuid.uuid4()),
                },
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )

    assert response.status_code == 201
    data = response.json()['data']
    job = tenant.product_parse_jobs.get(pk=data['job_id'])
    assert data['generate_after'] is True
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.products.tasks.parse_single_part_then_generate_description',
        args=[job.pk],
    ).count() == 1


@pytest.mark.django_db
def test_parse_retry_returns_canonical_jobs_and_conflicting_intent_is_409(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('parse-idempotent-api')
    product = make_product(tenant)
    client = Client()
    key = str(uuid.uuid4())
    common = {
        'product_id': product.pk,
        'source': 'tachka',
        'idempotency_key': key,
    }

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            first = client.post(
                '/api/v1/products/parse/',
                common,
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )
            retry = client.post(
                '/api/v1/products/parse/',
                common,
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )
            conflict = client.post(
                '/api/v1/products/parse/',
                {**common, 'generate_after': True},
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )

    assert first.status_code == 201
    assert retry.status_code == 201
    assert retry.json()['data'] == first.json()['data']
    assert conflict.status_code == 409
    assert tenant.product_parse_jobs.count() == 1
    assert BackgroundJobDispatch.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_parse_retries_create_one_canonical_job():
    tenant, api_key = make_tenant('parse-concurrent-api')
    product = make_product(tenant)
    idempotency_key = str(uuid.uuid4())
    barrier = threading.Barrier(2)

    def submit():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            response = Client().post(
                '/api/v1/products/parse/',
                {
                    'product_id': product.pk,
                    'source': 'tachka',
                    'idempotency_key': idempotency_key,
                },
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )
            return response.status_code, response.json()
        finally:
            connections.close_all()

    with patch(
        'apps.core.tasks.execute_background_dispatch.apply_async',
    ), ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))

    assert [status_code for status_code, _ in results] == [201, 201]
    assert results[0][1]['data'] == results[1][1]['data']
    assert tenant.product_parse_jobs.count() == 1
    assert BackgroundJobDispatch.objects.count() == 1


@pytest.mark.django_db
def test_parse_retry_survives_mutated_product_and_source_registry(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('parse-stable-retry-api')
    product = make_product(tenant, brand='OLD-BRAND')
    client = Client()
    key = str(uuid.uuid4())
    payload = {'product_id': product.pk, 'idempotency_key': key}

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            first = client.post(
                '/api/v1/products/parse/',
                payload,
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )
    product.brand = 'NEW-BRAND'
    product.save(update_fields=['brand', 'updated_at'])
    with patch(
        'apps.products.views.get_part_source_policies',
        side_effect=AssertionError('retry re-resolved mutable source registry'),
    ):
        retry = client.post(
            '/api/v1/products/parse/',
            payload,
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {api_key}',
        )

    assert first.status_code == 201
    assert retry.status_code == 201
    assert retry.json()['data'] == first.json()['data']
    assert tenant.product_parse_jobs.count() == 3


@pytest.mark.django_db
def test_expired_terminal_parse_intent_allows_new_attempt(
    django_capture_on_commit_callbacks,
    settings,
):
    from apps.core.retention import purge_retained_data
    from apps.products.models import ProductParseIntent

    settings.PRODUCT_PARSE_JOB_RETENTION_DAYS = 180
    settings.RETENTION_PURGE_BATCH_SIZE = 100
    tenant, api_key = make_tenant('parse-expired-intent-api')
    product = make_product(tenant)
    client = Client()
    key = str(uuid.uuid4())
    payload = {
        'product_id': product.pk,
        'source': 'tachka',
        'idempotency_key': key,
    }
    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            first = client.post(
                '/api/v1/products/parse/',
                payload,
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )
    intent = ProductParseIntent.objects.get(idempotency_key=key)
    job = intent.jobs.get()
    old = timezone.now() - timedelta(days=181)
    job.status = ProductParseJob.Status.SUCCESS
    job.save(update_fields=['status', 'updated_at'])
    ProductParseIntent.objects.filter(pk=intent.pk).update(created_at=old)
    ProductParseJob.objects.filter(pk=job.pk).update(created_at=old)

    purge_retained_data()
    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            second = client.post(
                '/api/v1/products/parse/',
                payload,
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()['data']['job_id'] != first.json()['data']['job_id']


@pytest.mark.django_db
def test_regenerate_endpoint_uses_enrichment_pipeline(django_capture_on_commit_callbacks):
    tenant, api_key = make_tenant('regenerate-enriched-api')
    product = make_product(tenant)
    client = Client()

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                f'/api/v1/products/{product.pk}/regenerate/',
                {'idempotency_key': '10000000-0000-4000-8000-000000000001'},
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )

    assert response.status_code == 202
    data = response.json()['data']
    job = tenant.product_parse_jobs.get(pk=data['job_id'])
    assert job.product == product
    assert job.normalized_article == normalize_part_code(product.article)
    assert data['generate_after'] is True
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.products.tasks.parse_single_part_then_generate_description',
        args=[job.pk],
    ).count() == 1


@pytest.mark.django_db
def test_regenerate_endpoint_rejects_other_tenant_product():
    tenant_a, api_key = make_tenant('regenerate-owner')
    tenant_b, _ = make_tenant('regenerate-other')
    product_b = make_product(tenant_b)
    client = Client()

    response = client.post(
        f'/api/v1/products/{product_b.pk}/regenerate/',
        {'idempotency_key': '10000000-0000-4000-8000-000000000002'},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 404
    assert tenant_a.product_parse_jobs.count() == 0


@pytest.mark.django_db
def test_parse_endpoint_rejects_other_tenant_product():
    tenant_a, api_key = make_tenant('parse-owner')
    tenant_b, _ = make_tenant('parse-other')
    product_b = make_product(tenant_b)
    client = Client()

    response = client.post(
        '/api/v1/products/parse/',
        {
            'product_id': product_b.pk,
            'idempotency_key': str(uuid.uuid4()),
        },
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 404
    assert tenant_a.product_parse_jobs.count() == 0


@pytest.mark.django_db
def test_fitment_review_rejects_and_refreshes_product_applicability():
    tenant, _ = make_tenant('fitment-review-api')
    product = make_product(tenant)
    fitment = VehicleFitment.objects.create(
        tenant=tenant,
        product=product,
        source_id='tachka',
        make='BMW',
        model='5',
        generation='G30',
        confidence=1.0,
        needs_review=False,
    )
    ProductEnrichmentService.refresh_product_denormalized_enrichment(product)
    product.save(update_fields=['oem_numbers', 'cross_numbers', 'applicability'])
    assert product.applicability

    response = owner_client(tenant).post(
        f'/api/v1/products/{product.pk}/fitments/{fitment.pk}/reject/',
        content_type='application/json',
    )

    assert response.status_code == 200
    fitment.refresh_from_db()
    product.refresh_from_db()
    assert fitment.review_status == ReviewStatus.REJECTED
    assert fitment.needs_review is False
    assert product.applicability == []


@pytest.mark.django_db
def test_fitment_review_rejects_other_tenant_record():
    tenant_a, _ = make_tenant('fitment-review-owner')
    tenant_b, _ = make_tenant('fitment-review-other')
    product_a = make_product(tenant_a)
    product_b = make_product(tenant_b)
    fitment_b = VehicleFitment.objects.create(
        tenant=tenant_b,
        product=product_b,
        source_id='tachka',
        make='BMW',
        model='5',
        confidence=1.0,
        needs_review=True,
    )

    response = owner_client(tenant_a).post(
        f'/api/v1/products/{product_a.pk}/fitments/{fitment_b.pk}/approve/',
        content_type='application/json',
    )

    assert response.status_code == 404
    fitment_b.refresh_from_db()
    assert fitment_b.review_status == ReviewStatus.PENDING


@pytest.mark.django_db
def test_review_queue_lists_tenant_scoped_pending_items():
    tenant, _ = make_tenant('review-queue-owner')
    other_tenant, _ = make_tenant('review-queue-other')
    product = make_product(tenant, name='Колодки BREMBO P50136')
    other_product = make_product(other_tenant, article='P2', name='Колодки TRW P2')
    fitment = VehicleFitment.objects.create(
        tenant=tenant,
        product=product,
        source_id='tachka',
        make='BMW',
        model='5',
        generation='G30',
        confidence=0.4,
        needs_review=True,
    )
    fact = ProductEnrichmentFact.objects.create(
        tenant=tenant,
        product=product,
        source_id='tachka',
        fact_type=ProductEnrichmentFact.FactType.DESCRIPTION_HINT,
        name='Спорный факт',
        value='требует проверки',
        confidence=0.4,
        needs_review=True,
    )
    VehicleFitment.objects.create(
        tenant=other_tenant,
        product=other_product,
        source_id='tachka',
        make='AUDI',
        model='A6',
        confidence=0.4,
        needs_review=True,
    )
    response = owner_client(tenant).get('/api/v1/products/review-queue/')

    assert response.status_code == 200
    data = response.json()['data']
    ids = {item['id'] for item in data}
    assert ids == {f'fitment:{fitment.pk}', f'fact:{fact.pk}'}
    assert all(item['product']['id'] == product.pk for item in data)


@pytest.mark.django_db
def test_review_queue_rejects_invalid_product_id_without_server_error():
    tenant, _ = make_tenant('review-queue-invalid-product')

    response = owner_client(tenant).get(
        '/api/v1/products/review-queue/?product_id=not-an-integer',
    )

    assert response.status_code == 400
    assert response.json() == {'status': 'error', 'code': 'bad_product_id'}


@pytest.mark.django_db
def test_review_queue_paginates_refs_before_serializing_large_cardinality(
    django_assert_num_queries,
):
    tenant, _ = make_tenant('review-queue-db-page')
    product = make_product(tenant, name='Товар для большой очереди')
    shared_updated_at = timezone.now() - timedelta(minutes=1)

    fitments = VehicleFitment.objects.bulk_create([
        VehicleFitment(
            tenant=tenant,
            product=product,
            source_id='tachka',
            make='BMW',
            model=f'Model {index}',
            confidence=0.4,
            needs_review=True,
        )
        for index in range(80)
    ])
    facts = ProductEnrichmentFact.objects.bulk_create([
        ProductEnrichmentFact(
            tenant=tenant,
            product=product,
            source_id='tachka',
            fact_type=ProductEnrichmentFact.FactType.TECHNICAL,
            name=f'Fact {index}',
            value=f'Value {index}',
            confidence=0.4,
            needs_review=True,
        )
        for index in range(80)
    ])
    ProductCatalogClassification.objects.create(
        tenant=tenant,
        product=product,
        domain=ProductCatalogClassification.Domain.AUTO_PARTS,
        source=ProductCatalogClassification.Source.RULES,
        reason='Tie-break classification',
        confidence=0.4,
        needs_review=True,
    )
    VehicleFitment.objects.filter(pk__in=[item.pk for item in fitments]).update(
        updated_at=shared_updated_at,
    )
    ProductEnrichmentFact.objects.filter(pk__in=[item.pk for item in facts]).update(
        updated_at=shared_updated_at,
    )
    ProductCatalogClassification.objects.filter(product=product).update(
        updated_at=shared_updated_at,
    )
    newest_updated_at = timezone.now()
    VehicleFitment.objects.filter(pk=fitments[0].pk).update(
        updated_at=newest_updated_at,
    )
    ProductEnrichmentFact.objects.filter(pk=facts[0].pk).update(
        updated_at=newest_updated_at,
    )
    classification = ProductCatalogClassification.objects.get(product=product)
    ProductCatalogClassification.objects.filter(pk=classification.pk).update(
        updated_at=newest_updated_at,
    )

    client = owner_client(tenant)
    with patch('apps.products.views._serialize_review_item', wraps=_serialize_review_item) \
            as serialize_item, django_assert_num_queries(11):
        response = client.get(
            '/api/v1/products/review-queue/?page_size=7&page=1',
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload['meta']['total'] == 161
    assert payload['meta']['page'] == 1
    assert len(payload['data']) == 7
    assert serialize_item.call_count == 7
    assert [item['id'] for item in payload['data']] == [
        f'classification:{classification.pk}',
        f'fact:{facts[0].pk}',
        f'fitment:{fitments[0].pk}',
        *[f'fact:{item.pk}' for item in facts[1:5]],
    ]


@pytest.mark.django_db
def test_review_queue_lists_pending_classifications():
    tenant, _ = make_tenant('review-queue-classification-list')
    product = make_product(tenant, name='Колодки BREMBO P50136')
    category = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Ручная категория очереди',
        root_domain=CatalogDomain.objects.get(slug='auto_parts'),
        domain=TenantCatalogCategory.Domain.AUTO_PARTS,
    )
    product.catalog_category = category
    product.save(update_fields=['catalog_category', 'updated_at'])
    classification = ProductCatalogClassification.objects.create(
        tenant=tenant,
        product=product,
        domain=ProductCatalogClassification.Domain.AUTO_PARTS,
        confidence=0.75,
        source=ProductCatalogClassification.Source.RULES,
        reason='Найдены признаки автозапчасти.',
        needs_review=True,
    )
    response = owner_client(tenant).get('/api/v1/products/review-queue/')

    assert response.status_code == 200
    data = response.json()['data']
    assert data[0]['id'] == f'classification:{classification.pk}'
    assert data[0]['title'] == 'Автозапчасть'
    assert data[0]['reason'] == 'Найдены признаки автозапчасти.'
    assert data[0]['product']['catalog_category_id'] == category.pk


@pytest.mark.django_db
def test_review_queue_approves_fitment_and_refreshes_applicability():
    tenant, _ = make_tenant('review-queue-approve-fitment')
    product = make_product(tenant)
    fitment = VehicleFitment.objects.create(
        tenant=tenant,
        product=product,
        source_id='tachka',
        make='MERCEDES-BENZ',
        model='E-CLASS',
        generation='W213',
        confidence=0.4,
        needs_review=True,
    )
    client = owner_client(tenant)

    response = client.post(
        f'/api/v1/products/review-queue/fitment/{fitment.pk}/approve/',
        content_type='application/json',
    )

    assert response.status_code == 200
    fitment.refresh_from_db()
    product.refresh_from_db()
    assert fitment.review_status == ReviewStatus.APPROVED
    assert fitment.needs_review is False
    assert product.applicability[0]['model'] == 'E-CLASS'
    learned = GlobalPartFitment.objects.get(
        part__normalized_brand='BREMBO',
        part__normalized_article='P50136',
        source_id='human_review',
        model='E-CLASS',
    )
    assert learned.needs_review is False
    assert learned.confidence == 1.0

    consumer_tenant, _ = make_tenant('review-fitment-consumer')
    consumer_product = make_product(consumer_tenant)
    assert ProductKnowledgeGraphService.apply_known_fitments_to_product(consumer_product) == 1
    assert consumer_product.fitments.filter(
        make='MERCEDES-BENZ', model='E-CLASS', generation='W213',
    ).exists()


@pytest.mark.django_db
def test_review_queue_cannot_approve_unknown_classification():
    tenant, _ = make_tenant('review-queue-unknown-classification')
    product = make_product(tenant, name='Неясный товар')
    classification = ProductCatalogClassification.objects.create(
        tenant=tenant,
        product=product,
        domain=ProductCatalogClassification.Domain.UNKNOWN,
        confidence=0.3,
        source=ProductCatalogClassification.Source.RULES,
        reason='Не найдено достаточно признаков.',
        needs_review=True,
    )
    client = owner_client(tenant)

    response = client.post(
        f'/api/v1/products/review-queue/classification/{classification.pk}/approve/',
        content_type='application/json',
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'unknown_classification'
    classification.refresh_from_db()
    assert classification.review_status == ReviewStatus.PENDING


@pytest.mark.django_db
def test_catalog_classification_review_approve_marks_manual():
    tenant, _ = make_tenant('classification-review-api')
    product = make_product(tenant, name='Колодки тормозные BREMBO')
    classification = ProductEnrichmentService.classify_product_catalog_domain(product)
    classification.needs_review = True
    classification.save(update_fields=['needs_review', 'updated_at'])

    response = owner_client(tenant).post(
        f'/api/v1/products/{product.pk}/catalog-classification/approve/',
        content_type='application/json',
    )

    assert response.status_code == 200
    classification.refresh_from_db()
    assert classification.review_status == ReviewStatus.APPROVED
    assert classification.needs_review is False
    assert classification.source == ProductCatalogClassification.Source.MANUAL


@pytest.mark.django_db
def test_catalog_classification_review_cannot_approve_unknown_domain():
    tenant, _ = make_tenant('classification-review-unknown')
    product = make_product(
        tenant,
        article='ITEM1',
        brand='NO_BRAND',
        name='Товар без понятной категории',
    )
    classification = ProductEnrichmentService.classify_product_catalog_domain(product)

    response = owner_client(tenant).post(
        f'/api/v1/products/{product.pk}/catalog-classification/approve/',
        content_type='application/json',
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'unknown_classification'
    classification.refresh_from_db()
    assert classification.review_status == ReviewStatus.PENDING


@pytest.mark.django_db
def test_assign_catalog_category_replaces_previous_unknown_with_manual_classification():
    tenant, _ = make_tenant('classification-force-after-category')
    product = make_product(
        tenant,
        article='ITEM2',
        brand='NO_BRAND',
        name='Товар без понятной категории',
    )
    classification = ProductEnrichmentService.classify_product_catalog_domain(product)
    classification.source = ProductCatalogClassification.Source.MANUAL
    classification.review_status = ReviewStatus.APPROVED
    classification.save(update_fields=['source', 'review_status', 'updated_at'])
    category = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Ходовая часть',
        root_domain=CatalogDomain.objects.get(slug='auto_parts'),
        domain=ProductCatalogClassification.Domain.AUTO_PARTS,
    )

    response = owner_client(tenant).post(
        '/api/v1/products/catalog-categories/assign/',
        {'product_ids': [product.pk], 'catalog_category': category.pk},
        content_type='application/json',
    )

    assert response.status_code == 200
    classification.refresh_from_db()
    assert classification.domain == ProductCatalogClassification.Domain.AUTO_PARTS
    assert classification.confidence == 0.95
    assert classification.source == ProductCatalogClassification.Source.MANUAL
    assert classification.review_status == ReviewStatus.APPROVED
    assert classification.needs_review is False


@pytest.mark.django_db
def test_parse_endpoint_rejects_non_auto_parts_tenant():
    tenant, api_key = make_tenant('parse-jewellery')
    tenant.catalog_domain = 'jewellery'
    tenant.save(update_fields=['catalog_domain'])
    product = make_product(tenant)
    client = Client()

    response = client.post(
        '/api/v1/products/parse/',
        {
            'product_id': product.pk,
            'idempotency_key': str(uuid.uuid4()),
        },
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'auto_parts_enrichment_disabled'
    assert tenant.product_parse_jobs.count() == 0


@pytest.mark.django_db
def test_parse_endpoint_allows_enabled_auto_parts_domain_for_mixed_tenant(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('parse-jewellery-with-auto-parts', catalog_domain='jewellery')
    from apps.products.services import ProductCategorySeedService
    ProductCategorySeedService.enable_tenant_catalog_domain(tenant, 'auto_parts')
    root_domain = CatalogDomain.objects.get(slug='auto_parts')
    category = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Тормозные колодки',
        root_domain=root_domain,
        domain=ProductCatalogClassification.Domain.AUTO_PARTS,
    )
    product = make_product(tenant, category_1c='Тормозные колодки')
    product.catalog_category = category
    product.save(update_fields=['catalog_category', 'updated_at'])
    ProductCatalogClassification.objects.create(
        tenant=tenant,
        product=product,
        domain=ProductCatalogClassification.Domain.AUTO_PARTS,
        confidence=0.85,
        source=ProductCatalogClassification.Source.RULES,
        reason='Тип товара определён по категории каталога: Тормозные колодки.',
        needs_review=False,
    )
    client = Client()

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                '/api/v1/products/parse/',
                {
                    'product_id': product.pk,
                    'idempotency_key': str(uuid.uuid4()),
                },
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )

    assert response.status_code == 201
    data = response.json()['data']
    job = tenant.product_parse_jobs.get(pk=data['job_id'])
    assert job.product == product
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.products.tasks.parse_single_part',
        args=[job.pk],
    ).exists()
    jobs = tenant.product_parse_jobs.filter(product=product)
    assert set(jobs.values_list('source_id', flat=True)) == {'tachka', 'rossko', 'euroauto'}
    assert BackgroundJobDispatch.objects.count() == 3


@pytest.mark.django_db
def test_regenerate_endpoint_for_non_auto_parts_tenant_queues_ai_without_enrichment(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('regenerate-jewellery')
    tenant.catalog_domain = 'jewellery'
    tenant.save(update_fields=['catalog_domain'])
    product = make_product(tenant)
    client = Client()

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                f'/api/v1/products/{product.pk}/regenerate/',
                {'idempotency_key': '10000000-0000-4000-8000-000000000003'},
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )

    assert response.status_code == 202
    data = response.json()['data']
    assert data['job_id'] is None
    assert data['state'] == 'queued'
    assert tenant.product_parse_jobs.count() == 0
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.ai_agent.tasks.generate_description_task',
        args=[product.pk],
    ).count() == 1


@pytest.mark.django_db
def test_regenerate_endpoint_reuses_one_paid_intent(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('regenerate-idempotent')
    tenant.catalog_domain = 'jewellery'
    tenant.save(update_fields=['catalog_domain'])
    product = make_product(tenant)
    client = Client()
    payload = {'idempotency_key': '20000000-0000-4000-8000-000000000001'}

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            first = client.post(
                f'/api/v1/products/{product.pk}/regenerate/',
                payload,
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )
            second = client.post(
                f'/api/v1/products/{product.pk}/regenerate/',
                payload,
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )

    assert first.status_code == second.status_code == 202
    dispatches = BackgroundJobDispatch.objects.filter(
        task_name='apps.ai_agent.tasks.generate_description_task',
        args=[product.pk],
    )
    assert dispatches.count() == 1
    assert str(payload['idempotency_key']) in dispatches.get().deduplication_key


@pytest.mark.django_db
def test_regenerate_parser_charges_parse_budget_once(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('regenerate-parse-budget')
    product = make_product(tenant)
    client = Client()
    payload = {
        'idempotency_key': str(uuid.uuid4()),
        'source': 'euroauto',
    }

    with patch(
        'apps.products.views.consume_transactional_tenant_daily_budget',
    ) as consume, patch(
        'apps.core.tasks.execute_background_dispatch.apply_async',
    ), django_capture_on_commit_callbacks(execute=True):
        first = client.post(
            f'/api/v1/products/{product.pk}/regenerate/', payload,
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {api_key}',
        )
        retry = client.post(
            f'/api/v1/products/{product.pk}/regenerate/', payload,
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {api_key}',
        )

    assert first.status_code == retry.status_code == 202
    assert retry.json() == first.json()
    consume.assert_called_once_with(
        tenant=tenant,
        scope='product-parse-jobs',
        cost=1,
        limit=settings.PRODUCT_PARSE_TENANT_DAILY_JOBS,
    )


@pytest.mark.django_db
def test_regenerate_parse_budget_exhaustion_rolls_back_job_and_dispatch():
    from rest_framework.exceptions import Throttled

    tenant, api_key = make_tenant('regenerate-parse-exhausted')
    product = make_product(tenant)

    with patch(
        'apps.products.views.consume_transactional_tenant_daily_budget',
        side_effect=Throttled(wait=60),
    ), patch('apps.core.tasks.execute_background_dispatch.apply_async') as publish:
        response = Client().post(
            f'/api/v1/products/{product.pk}/regenerate/',
            {
                'idempotency_key': str(uuid.uuid4()),
                'source': 'euroauto',
            },
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {api_key}',
        )

    assert response.status_code == 429
    assert tenant.product_parse_jobs.count() == 0
    assert BackgroundJobDispatch.objects.count() == 0
    publish.assert_not_called()


@pytest.mark.django_db
def test_regenerate_retry_uses_original_mode_after_tenant_domain_changes(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('regenerate-mode-stable')
    tenant.catalog_domain = 'jewellery'
    tenant.save(update_fields=['catalog_domain'])
    product = make_product(tenant)
    client = Client()
    payload = {'idempotency_key': str(uuid.uuid4())}

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            first = client.post(
                f'/api/v1/products/{product.pk}/regenerate/',
                payload,
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )
    tenant.catalog_domain = 'auto_parts'
    tenant.save(update_fields=['catalog_domain'])
    with patch(
        'apps.products.views.ProductEnrichmentService.is_product_auto_part_candidate',
        side_effect=AssertionError('retry recomputed mutable mode'),
    ):
        retry = client.post(
            f'/api/v1/products/{product.pk}/regenerate/',
            payload,
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {api_key}',
        )

    assert first.status_code == 202
    assert retry.status_code == 202
    assert retry.json() == first.json()
    assert tenant.product_parse_jobs.count() == 0
    assert BackgroundJobDispatch.objects.count() == 1


@pytest.mark.django_db
def test_regenerate_retry_does_not_reresolve_removed_source(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('regenerate-source-stable')
    product = make_product(tenant)
    client = Client()
    payload = {
        'idempotency_key': str(uuid.uuid4()),
        'source': 'tachka',
    }

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            first = client.post(
                f'/api/v1/products/{product.pk}/regenerate/',
                payload,
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )
    with patch(
        'apps.products.views.get_part_source_config',
        side_effect=AssertionError('retry resolved mutable source registry'),
    ):
        retry = client.post(
            f'/api/v1/products/{product.pk}/regenerate/',
            payload,
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {api_key}',
        )

    assert first.status_code == 202
    assert retry.status_code == 202
    assert retry.json() == first.json()
    assert tenant.product_parse_jobs.count() == 1
    assert BackgroundJobDispatch.objects.count() == 1


@pytest.mark.django_db
def test_regenerate_rejects_reused_key_for_different_source(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('regenerate-source-conflict')
    product = make_product(tenant)
    client = Client()
    idempotency_key = '20000000-0000-4000-8000-000000000002'

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            first = client.post(
                f'/api/v1/products/{product.pk}/regenerate/',
                {'idempotency_key': idempotency_key, 'source': 'tachka'},
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )
            conflict = client.post(
                f'/api/v1/products/{product.pk}/regenerate/',
                {'idempotency_key': idempotency_key, 'source': 'rossko'},
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json() == {'status': 'error', 'code': 'idempotency_conflict'}
    assert tenant.product_parse_jobs.filter(product=product).count() == 1
    assert BackgroundJobDispatch.objects.filter(
        deduplication_key__contains=idempotency_key,
    ).count() == 1


@pytest.mark.django_db
def test_plain_ai_regenerate_rejects_reused_key_for_different_source(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('regenerate-plain-source-conflict')
    tenant.catalog_domain = 'jewellery'
    tenant.save(update_fields=['catalog_domain'])
    product = make_product(tenant)
    client = Client()
    idempotency_key = '20000000-0000-4000-8000-000000000003'

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            first = client.post(
                f'/api/v1/products/{product.pk}/regenerate/',
                {'idempotency_key': idempotency_key, 'source': 'tachka'},
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )
            conflict = client.post(
                f'/api/v1/products/{product.pk}/regenerate/',
                {'idempotency_key': idempotency_key, 'source': 'rossko'},
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json() == {'status': 'error', 'code': 'idempotency_conflict'}
    assert tenant.product_parse_jobs.filter(product=product).count() == 0
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.ai_agent.tasks.generate_description_task',
        args=[product.pk],
    ).count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_regenerate_reuses_canonical_parse_job_without_orphan():
    tenant, api_key = make_tenant('regenerate-concurrent')
    product = make_product(tenant)
    idempotency_key = '20000000-0000-4000-8000-000000000004'
    start_together = threading.Barrier(2)
    second_create_started = threading.Event()
    create_call_lock = threading.Lock()
    create_call_count = 0
    original_create_parse_job = ProductEnrichmentService.create_parse_job

    # Force the vulnerable implementation to let both requests observe a
    # missing dispatch before either creates one. With the product row lock,
    # request two cannot reach this method until request one commits.
    def coordinated_create_parse_job(*args, **kwargs):
        nonlocal create_call_count
        with create_call_lock:
            create_call_count += 1
            call_number = create_call_count
        if call_number == 1:
            second_create_started.wait(timeout=2)
        else:
            second_create_started.set()
        return original_create_parse_job(*args, **kwargs)

    def regenerate():
        close_old_connections()
        try:
            client = Client()
            start_together.wait(timeout=10)
            response = client.post(
                f'/api/v1/products/{product.pk}/regenerate/',
                {'idempotency_key': idempotency_key, 'source': 'tachka'},
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )
            return response.status_code, response.json()
        finally:
            connections.close_all()

    with patch(
        'apps.core.tasks.execute_background_dispatch.apply_async',
    ), patch.object(
        ProductEnrichmentService,
        'create_parse_job',
        side_effect=coordinated_create_parse_job,
    ), ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: regenerate(), range(2)))

    assert [status_code for status_code, _ in results] == [202, 202]
    assert results[0][1] == results[1][1]
    assert create_call_count == 1
    jobs = tenant.product_parse_jobs.filter(product=product)
    assert jobs.count() == 1
    job = jobs.get()
    dispatch = BackgroundJobDispatch.objects.get(
        deduplication_key__contains=idempotency_key,
    )
    assert dispatch.args == [job.pk]


@pytest.mark.django_db
def test_parse_endpoint_rejects_non_auto_parts_product_for_mixed_tenant():
    tenant, api_key = make_tenant('parse-mixed-jewellery')
    tenant.catalog_domain = 'mixed'
    tenant.save(update_fields=['catalog_domain'])
    product = make_product(
        tenant,
        article='RING1',
        brand='NO_BRAND',
        name='Золотое кольцо',
        category_1c='Украшения',
    )
    client = Client()

    response = client.post(
        '/api/v1/products/parse/',
        {
            'product_id': product.pk,
            'idempotency_key': str(uuid.uuid4()),
        },
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'product_is_not_auto_part'
    assert tenant.product_parse_jobs.count() == 0
    product.refresh_from_db()
    assert product.catalog_classification.domain == ProductCatalogClassification.Domain.JEWELLERY


@pytest.mark.django_db
def test_regenerate_endpoint_for_mixed_non_auto_part_queues_ai_without_enrichment(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('regenerate-mixed-jewellery')
    tenant.catalog_domain = 'mixed'
    tenant.save(update_fields=['catalog_domain'])
    product = make_product(
        tenant,
        article='RING2',
        brand='NO_BRAND',
        name='Серебряное кольцо',
        category_1c='Украшения',
    )
    client = Client()

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                f'/api/v1/products/{product.pk}/regenerate/',
                {'idempotency_key': '10000000-0000-4000-8000-000000000004'},
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )

    assert response.status_code == 202
    assert response.json()['data']['job_id'] is None
    assert tenant.product_parse_jobs.count() == 0
    product.refresh_from_db()
    assert product.catalog_classification.domain == ProductCatalogClassification.Domain.JEWELLERY
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.ai_agent.tasks.generate_description_task',
        args=[product.pk],
    ).count() == 1


@pytest.mark.django_db
def test_fitments_and_cross_codes_are_tenant_scoped():
    tenant, api_key = make_tenant('enrichment-read')
    product = make_product(tenant)
    ProductEnrichmentService.create_cross_code(
        tenant=tenant,
        product=product,
        manufacturer='MERCEDES-BENZ',
        code='A 000 420 60 00',
        normalized_code='A0004206000',
        code_type=ProductCrossCode.CodeType.OEM,
    )
    ProductEnrichmentService.create_fitment(
        tenant=tenant,
        product=product,
        make='MERCEDES-BENZ',
        model='E-CLASS',
        generation='W213',
    )
    client = Client()

    cross_response = client.get(
        f'/api/v1/products/{product.pk}/cross-codes/',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )
    fitments_response = client.get(
        f'/api/v1/products/{product.pk}/fitments/',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert cross_response.status_code == 200
    assert cross_response.json()['data'][0]['normalized_code'] == 'A0004206000'
    assert fitments_response.status_code == 200
    assert fitments_response.json()['data'][0]['model'] == 'E-CLASS'


@pytest.mark.django_db
def test_products_list_can_filter_by_catalog_classification():
    tenant, api_key = make_tenant('catalog-filter')
    auto_part = make_product(tenant, name='Колодки тормозные BREMBO P50136')
    unknown = make_product(tenant, article='UNKNOWN1', name='Товар без классификации')
    jewellery = make_product(
        tenant,
        article='RING1',
        brand='NO_BRAND',
        name='Золотое кольцо',
        category_1c='Украшения',
    )
    ProductEnrichmentService.classify_product_catalog_domain(auto_part)
    ProductEnrichmentService.classify_product_catalog_domain(jewellery)
    client = Client()

    response = client.get(
        '/api/v1/products/?catalog_domain=auto_parts',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 200
    ids = [item['id'] for item in response.json()['data']]
    assert ids == [auto_part.pk]
    assert response.json()['meta']['domain_counts']['all'] == 3
    assert response.json()['meta']['domain_counts']['auto_parts'] == 1
    assert response.json()['meta']['domain_counts']['unknown'] == 1

    unknown_response = client.get(
        '/api/v1/products/?catalog_domain=unknown',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert unknown_response.status_code == 200
    unknown_ids = [item['id'] for item in unknown_response.json()['data']]
    assert unknown_ids == [unknown.pk]


@pytest.mark.django_db
def test_tenant_catalog_category_api_crud_and_mapping():
    tenant, api_key = make_tenant('catalog-category-api')
    product = make_product(tenant, category_1c='Тормоза')
    client = Client()

    create_response = client.post(
        '/api/v1/products/catalog-categories/',
        {
            'name': 'Тормозные колодки',
            'root_domain': CatalogDomain.objects.get(slug='auto_parts').pk,
            'domain': 'auto_parts',
            'aliases': [],
        },
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )
    assert create_response.status_code == 201
    category_id = create_response.json()['data']['id']
    category = TenantCatalogCategory.objects.get(pk=category_id)
    assert category.tenant == tenant
    assert category.root_domain.slug == 'auto_parts'
    assert category.normalized_name

    mapping_response = client.post(
        '/api/v1/products/catalog-category-mappings/',
        {'source_category': 'Тормоза', 'category': category_id},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )
    assert mapping_response.status_code == 201
    assert TenantCategoryMapping.objects.filter(
        tenant=tenant, source_category='Тормоза', category=category,
    ).exists()

    source_response = client.get(
        '/api/v1/products/catalog-source-categories/',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )
    assert source_response.status_code == 200
    assert source_response.json()['data'] == [
        {'source_category': product.category_1c, 'catalog_category': category_id},
    ]

    mapping = TenantCategoryMapping.objects.get(tenant=tenant, source_category='Тормоза')
    delete_response = client.delete(
        f'/api/v1/products/catalog-category-mappings/{mapping.pk}/',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert delete_response.status_code == 204
    assert not TenantCategoryMapping.objects.filter(pk=mapping.pk).exists()


@pytest.mark.django_db
def test_catalog_domains_endpoint_returns_active_platform_domains():
    tenant, _ = make_tenant('catalog-domains-api')
    CatalogDomain.objects.create(
        slug='custom_goods',
        name='Спецтовары',
        short_name='Спец',
        seo_title='Спецтовары купить',
        seo_description='Каталог специальных товаров.',
        is_active=True,
        sort_order=5,
    )
    CatalogDomain.objects.create(slug='hidden', name='Скрытый домен', is_active=False)
    client = owner_client(tenant)

    response = client.get('/api/v1/catalog-domains/')

    assert response.status_code == 200
    data = response.json()['data']
    slugs = [item['slug'] for item in data]
    assert 'custom_goods' in slugs
    assert 'electronics' in slugs
    assert 'hidden' not in slugs
    assert 'mixed' not in slugs
    assert 'unknown' not in slugs
    custom_goods = next(item for item in data if item['slug'] == 'custom_goods')
    assert custom_goods['seo_title'] == 'Спецтовары купить'


@pytest.mark.django_db
def test_enabling_catalog_domain_seeds_tenant_categories():
    tenant, _ = make_tenant('catalog-domain-enable')
    client = owner_client(tenant)

    response = client.post(
        '/api/v1/catalog-domains/',
        {'domain_slug': 'electronics', 'is_enabled': True},
        content_type='application/json',
    )

    assert response.status_code == 200
    assert tenant.enabled_catalog_domains.filter(
        domain__slug='electronics',
        is_enabled=True,
    ).exists()
    assert tenant.catalog_categories.filter(
        root_domain__slug='electronics',
        name='Смартфоны и телефоны',
    ).exists()
    assert tenant.catalog_categories.filter(
        root_domain__slug='electronics',
        parent__name='Смартфоны и телефоны',
        name='Смартфоны',
    ).exists()


@pytest.mark.django_db
def test_tenant_catalog_category_default_image_is_product_fallback():
    tenant, api_key = make_tenant('catalog-category-image')
    category = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Ходовая часть',
        root_domain=CatalogDomain.objects.get(slug='auto_parts'),
        domain='auto_parts',
    )
    product = make_product(tenant, category_1c='Ходовая')
    product.catalog_category = category
    product.save(update_fields=['catalog_category'])
    client = Client()

    from PIL import Image
    image_bytes = io.BytesIO()
    Image.new('RGB', (32, 32), 'white').save(image_bytes, format='JPEG')
    image = SimpleUploadedFile(
        'fallback.jpg',
        image_bytes.getvalue(),
        content_type='image/jpeg',
    )
    upload_response = client.post(
        f'/api/v1/products/catalog-categories/{category.pk}/default-image/',
        {'image': image},
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert upload_response.status_code == 200
    category.refresh_from_db()
    assert category.default_image_s3_key
    assert category.default_image_source_name == 'fallback.jpg'

    list_response = client.get('/api/v1/products/', HTTP_AUTHORIZATION=f'Bearer {api_key}')

    assert list_response.status_code == 200
    data = list_response.json()['data'][0]
    assert data['images_count'] == 0
    assert data['primary_thumb_url']


@pytest.mark.django_db
def test_catalog_category_image_uses_actual_key_and_deletes_after_commit(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('catalog-category-storage-key')
    category = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Тестовая категория',
        root_domain=CatalogDomain.objects.get(slug='auto_parts'),
        domain='auto_parts',
    )
    from PIL import Image
    image_bytes = io.BytesIO()
    Image.new('RGB', (32, 32), 'white').save(image_bytes, format='JPEG')
    image = SimpleUploadedFile('fallback.jpg', image_bytes.getvalue(), content_type='image/jpeg')
    client = Client()

    with patch(
        'apps.products.views.default_storage.save',
        return_value='dev/catalog-categories/actual_suffixed.jpg',
    ):
        response = client.post(
            f'/api/v1/products/catalog-categories/{category.pk}/default-image/',
            {'image': image},
            HTTP_AUTHORIZATION=f'Bearer {api_key}',
        )

    assert response.status_code == 200
    category.refresh_from_db()
    assert category.default_image_s3_key == 'dev/catalog-categories/actual_suffixed.jpg'

    with patch(
        'apps.core.storage.default_storage.delete',
    ) as storage_delete, django_capture_on_commit_callbacks(execute=True):
        response = client.delete(
            f'/api/v1/products/catalog-categories/{category.pk}/default-image/',
            HTTP_AUTHORIZATION=f'Bearer {api_key}',
        )
        storage_delete.assert_not_called()

    assert response.status_code == 200
    storage_delete.assert_called_once_with('dev/catalog-categories/actual_suffixed.jpg')


@pytest.mark.django_db
def test_hard_deleted_catalog_category_removes_fallback_after_commit(
    django_capture_on_commit_callbacks,
):
    tenant, _ = make_tenant('catalog-category-hard-delete')
    category = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Удаляемая категория',
        root_domain=CatalogDomain.objects.get(slug='auto_parts'),
        domain='auto_parts',
        default_image_s3_key='dev/catalog-categories/delete/fallback.jpg',
    )

    with patch(
        'apps.core.storage.default_storage.delete',
    ) as storage_delete, django_capture_on_commit_callbacks(execute=True):
        category.delete()
        storage_delete.assert_not_called()

    storage_delete.assert_called_once_with('dev/catalog-categories/delete/fallback.jpg')


@pytest.mark.django_db
def test_assign_catalog_category_to_selected_products():
    tenant, api_key = make_tenant('catalog-category-assign')
    other_tenant, _ = make_tenant('catalog-category-assign-other')
    product = make_product(tenant, name='Амортизатор Toyota', category_1c='Старые категории')
    second_product = make_product(tenant, article='P50137', name='Колодки Brembo')
    other_product = make_product(other_tenant)
    category = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Амортизаторы',
        root_domain=CatalogDomain.objects.get(slug='auto_parts'),
        domain=TenantCatalogCategory.Domain.AUTO_PARTS,
    )
    client = Client()

    response = client.post(
        '/api/v1/products/catalog-categories/assign/',
        {
            'product_ids': [product.pk, second_product.pk, other_product.pk],
            'catalog_category': category.pk,
        },
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 200
    data = response.json()['data']
    assert data['updated_count'] == 2
    assert data['skipped_count'] == 1
    product.refresh_from_db()
    second_product.refresh_from_db()
    other_product.refresh_from_db()
    assert product.catalog_category == category
    assert second_product.catalog_category == category
    assert other_product.catalog_category is None
    assert product.category_1c == 'Старые категории'
    assert product.catalog_classification.domain == ProductCatalogClassification.Domain.AUTO_PARTS
    assert product.catalog_classification.confidence == 0.95
    assert product.catalog_classification.source == ProductCatalogClassification.Source.API_KEY
    assert product.catalog_classification.reason == (
        'Категория каталога выбрана через API Key: Амортизаторы.'
    )
    assert product.catalog_classification.needs_review is False
    assert product.catalog_classification.review_status == ReviewStatus.APPROVED
    assert product.catalog_classification.reviewed_at is not None

    detail_response = client.get(
        f'/api/v1/products/{product.pk}/',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )
    assert detail_response.status_code == 200
    serialized_classification = detail_response.json()['data']['catalog_classification']
    assert serialized_classification['confidence'] == 0.95
    assert serialized_classification['source'] == ProductCatalogClassification.Source.API_KEY


@pytest.mark.django_db
def test_unassign_catalog_category_persists_manual_clear_and_keeps_shared_mapping():
    tenant, api_key = make_tenant('catalog-category-unassign')
    product = make_product(
        tenant,
        name='Фильтр масляный CHERY',
        category_1c='Фильтры 1С',
    )
    category = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Масляные фильтры',
        root_domain=CatalogDomain.objects.get(slug='auto_parts'),
        domain=TenantCatalogCategory.Domain.AUTO_PARTS,
    )
    client = Client()
    headers = {'HTTP_AUTHORIZATION': f'Bearer {api_key}'}

    assign_response = client.post(
        '/api/v1/products/catalog-categories/assign/',
        {'product_ids': [product.pk], 'catalog_category': category.pk},
        content_type='application/json',
        **headers,
    )
    assert assign_response.status_code == 200
    assert TenantCategoryMapping.objects.filter(
        tenant=tenant,
        source_category='Фильтры 1С',
        category=category,
    ).exists()

    remove_response = client.post(
        '/api/v1/products/catalog-categories/assign/',
        {'product_ids': [product.pk], 'catalog_category': None},
        content_type='application/json',
        **headers,
    )

    assert remove_response.status_code == 200
    product.refresh_from_db()
    assert product.catalog_category_id is None
    assert product.catalog_category_manually_cleared is True
    assert ProductEnrichmentService.get_product_tenant_category(product) is None
    product.refresh_from_db()
    assert product.catalog_category_id is None
    assert product.catalog_classification.source == ProductCatalogClassification.Source.RULES
    assert product.catalog_classification.confidence != 0.95
    assert TenantCategoryMapping.objects.filter(
        tenant=tenant,
        source_category='Фильтры 1С',
        category=category,
    ).exists()

    detail_response = client.get(f'/api/v1/products/{product.pk}/', **headers)
    assert detail_response.status_code == 200
    assert detail_response.json()['data']['catalog_category'] is None

    reassign_response = client.post(
        '/api/v1/products/catalog-categories/assign/',
        {'product_ids': [product.pk], 'catalog_category': category.pk},
        content_type='application/json',
        **headers,
    )
    assert reassign_response.status_code == 200
    product.refresh_from_db()
    assert product.catalog_category == category
    assert product.catalog_category_manually_cleared is False


@pytest.mark.django_db
def test_assign_catalog_category_creates_source_mapping():
    """Ручное назначение категории запоминается как маппинг «категория 1С →
    категория каталога»: следующий импорт с той же категорией источника
    не будет гадать по названию товара."""
    tenant, api_key = make_tenant('catalog-category-assign-map')
    product = make_product(tenant, name='Амортизатор Toyota', category_1c='Ходовая часть 1С')
    no_source_product = make_product(tenant, article='P50138', name='Колодки Brembo')
    category = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Амортизаторы',
        root_domain=CatalogDomain.objects.get(slug='auto_parts'),
        domain=TenantCatalogCategory.Domain.AUTO_PARTS,
    )
    client = Client()

    response = client.post(
        '/api/v1/products/catalog-categories/assign/',
        {
            'product_ids': [product.pk, no_source_product.pk],
            'catalog_category': category.pk,
        },
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 200
    mapping = TenantCategoryMapping.objects.get(tenant=tenant, source_category='Ходовая часть 1С')
    assert mapping.category == category
    # Для товаров без категории источника маппинг не создаётся.
    assert TenantCategoryMapping.objects.filter(tenant=tenant).count() == 1


@pytest.mark.django_db
def test_bulk_action_endpoint_creates_throttled_tenant_job(django_capture_on_commit_callbacks):
    tenant, _ = make_tenant('bulk-api')
    other_tenant, _ = make_tenant('bulk-other')
    product = make_product(tenant)
    other_product = make_product(other_tenant)
    client = owner_client(tenant)

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                '/api/v1/products/bulk-actions/',
                {
                    'action': 'enrich_selected',
                    'product_ids': [product.pk, other_product.pk],
                    'batch_size': 1,
                    'pause_seconds': 30,
                    'idempotency_key': str(uuid.uuid4()),
                },
                content_type='application/json',
            )

    assert response.status_code == 201
    data = response.json()['data']
    job = tenant.product_bulk_action_jobs.get(pk=data['id'])
    assert job.product_ids == [product.pk]
    assert job.total_count == 1
    assert job.skipped_count == 1
    assert job.batch_size == 1
    assert job.pause_seconds == 30
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.products.tasks.process_bulk_product_action',
        args=[job.pk],
    ).count() == 1


@pytest.mark.django_db
def test_bulk_action_retry_returns_original_job_and_payload_conflict_is_409(
    django_capture_on_commit_callbacks,
):
    tenant, _ = make_tenant('bulk-idempotent-api')
    product = make_product(tenant)
    client = owner_client(tenant)
    key = str(uuid.uuid4())
    payload = {
        'action': 'enrich_selected',
        'product_ids': [product.pk],
        'batch_size': 1,
        'pause_seconds': 30,
        'idempotency_key': key,
    }

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            first = client.post(
                '/api/v1/products/bulk-actions/',
                payload,
                content_type='application/json',
            )
            retry = client.post(
                '/api/v1/products/bulk-actions/',
                payload,
                content_type='application/json',
            )
            conflict = client.post(
                '/api/v1/products/bulk-actions/',
                {**payload, 'pause_seconds': 31},
                content_type='application/json',
            )

    assert first.status_code == 201
    assert retry.status_code == 201
    assert retry.json()['data']['id'] == first.json()['data']['id']
    assert conflict.status_code == 409
    assert tenant.product_bulk_action_jobs.count() == 1
    assert BackgroundJobDispatch.objects.count() == 1


@pytest.mark.django_db
def test_bulk_find_images_charges_resolved_daily_budget_once(
    django_capture_on_commit_callbacks,
):
    tenant, _ = make_tenant('bulk-find-images-budget')
    other_tenant, _ = make_tenant('bulk-find-images-other')
    product = make_product(tenant)
    other_product = make_product(other_tenant)
    client = owner_client(tenant)
    key = str(uuid.uuid4())
    payload = {
        'action': 'find_images',
        'product_ids': [product.pk, other_product.pk],
        'batch_size': 1,
        'pause_seconds': 30,
        'idempotency_key': key,
    }

    with patch(
        'apps.products.views.consume_transactional_tenant_daily_budget',
    ) as consume, patch(
        'apps.core.tasks.execute_background_dispatch.apply_async',
    ), django_capture_on_commit_callbacks(execute=True):
        first = client.post(
            '/api/v1/products/bulk-actions/', payload,
            content_type='application/json',
        )
        retry = client.post(
            '/api/v1/products/bulk-actions/', payload,
            content_type='application/json',
        )

    assert first.status_code == 201
    assert retry.status_code == 201
    assert retry.json()['data']['id'] == first.json()['data']['id']
    consume.assert_called_once_with(
        tenant=tenant,
        scope='image-search-jobs',
        cost=1,
        limit=settings.IMAGE_SEARCH_TENANT_DAILY_JOBS,
    )


@pytest.mark.django_db
def test_bulk_find_images_budget_exhaustion_creates_no_job_or_dispatch():
    from rest_framework.exceptions import Throttled

    tenant, _ = make_tenant('bulk-find-images-exhausted')
    product = make_product(tenant)
    client = owner_client(tenant)

    with patch(
        'apps.products.views.consume_transactional_tenant_daily_budget',
        side_effect=Throttled(wait=60),
    ), patch('apps.core.tasks.execute_background_dispatch.apply_async') as publish:
        response = client.post(
            '/api/v1/products/bulk-actions/',
            {
                'action': 'find_images',
                'product_ids': [product.pk],
                'batch_size': 1,
                'pause_seconds': 30,
                'idempotency_key': str(uuid.uuid4()),
            },
            content_type='application/json',
        )

    assert response.status_code == 429
    assert tenant.product_bulk_action_jobs.count() == 0
    assert BackgroundJobDispatch.objects.count() == 0
    publish.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(('action', 'source'), [
    ('enrich_selected', None),
    ('enrich_selected', 'tachka'),
    ('enrich_selected', 'rossko'),
    ('enrich_selected', 'euroauto'),
    ('generate_descriptions', None),
])
def test_bulk_parse_capable_actions_charge_parse_budget_once(
    django_capture_on_commit_callbacks,
    action,
    source,
):
    tenant, _ = make_tenant(f'bulk-parse-budget-{action}-{source or "default"}')
    product = make_product(tenant)
    client = owner_client(tenant)
    payload = {
        'action': action,
        'product_ids': [product.pk],
        'batch_size': 1,
        'pause_seconds': 30,
        'idempotency_key': str(uuid.uuid4()),
    }
    if source is not None:
        payload['source'] = source

    with patch(
        'apps.products.views.consume_transactional_tenant_daily_budget',
    ) as consume, patch(
        'apps.core.tasks.execute_background_dispatch.apply_async',
    ), django_capture_on_commit_callbacks(execute=True):
        first = client.post(
            '/api/v1/products/bulk-actions/', payload,
            content_type='application/json',
        )
        retry = client.post(
            '/api/v1/products/bulk-actions/', payload,
            content_type='application/json',
        )

    assert first.status_code == retry.status_code == 201
    assert retry.json()['data']['id'] == first.json()['data']['id']
    consume.assert_called_once_with(
        tenant=tenant,
        scope='product-parse-jobs',
        cost=1,
        limit=settings.PRODUCT_PARSE_TENANT_DAILY_JOBS,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(('action', 'source'), [
    ('enrich_selected', None),
    ('enrich_selected', 'tachka'),
    ('enrich_selected', 'rossko'),
    ('generate_descriptions', None),
])
def test_bulk_parse_budget_exhaustion_rolls_back_job_and_dispatch(action, source):
    from rest_framework.exceptions import Throttled

    tenant, _ = make_tenant(f'bulk-parse-exhausted-{action}-{source or "default"}')
    product = make_product(tenant)
    client = owner_client(tenant)

    with patch(
        'apps.products.views.consume_transactional_tenant_daily_budget',
        side_effect=Throttled(wait=60),
    ), patch('apps.core.tasks.execute_background_dispatch.apply_async') as publish:
        payload = {
            'action': action,
            'product_ids': [product.pk],
            'batch_size': 1,
            'pause_seconds': 30,
            'idempotency_key': str(uuid.uuid4()),
        }
        if source is not None:
            payload['source'] = source
        response = client.post(
            '/api/v1/products/bulk-actions/',
            payload,
            content_type='application/json',
        )

    assert response.status_code == 429
    assert tenant.product_bulk_action_jobs.count() == 0
    assert BackgroundJobDispatch.objects.count() == 0
    publish.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_concurrent_bulk_action_retries_create_one_canonical_job():
    tenant, _ = make_tenant('bulk-concurrent-api')
    product = make_product(tenant)
    authorization = owner_client(tenant).defaults['HTTP_AUTHORIZATION']
    idempotency_key = str(uuid.uuid4())
    barrier = threading.Barrier(2)

    def submit():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            response = Client(HTTP_AUTHORIZATION=authorization).post(
                '/api/v1/products/bulk-actions/',
                {
                    'action': 'enrich_selected',
                    'product_ids': [product.pk],
                    'batch_size': 1,
                    'pause_seconds': 30,
                    'idempotency_key': idempotency_key,
                },
                content_type='application/json',
            )
            return response.status_code, response.json()
        finally:
            connections.close_all()

    with patch(
        'apps.core.tasks.execute_background_dispatch.apply_async',
    ), ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))

    assert [status_code for status_code, _ in results] == [201, 201]
    assert results[0][1]['data']['id'] == results[1][1]['data']['id']
    assert tenant.product_bulk_action_jobs.count() == 1
    assert BackgroundJobDispatch.objects.count() == 1
