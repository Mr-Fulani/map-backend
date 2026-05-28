from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client

from apps.products.enrichment import normalize_part_code
from apps.products.models import Product, ProductCatalogClassification, ProductCrossCode
from apps.products.services import ProductEnrichmentService
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, api_key = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
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
def test_parse_endpoint_creates_tenant_scoped_job(django_capture_on_commit_callbacks):
    tenant, api_key = make_tenant('parse-api')
    product = make_product(tenant)
    client = Client()

    with patch('apps.products.tasks.parse_single_part.delay') as delay:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                '/api/v1/products/parse/',
                {'product_id': product.pk},
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )

    assert response.status_code == 201
    data = response.json()['data']
    job = tenant.product_parse_jobs.get(pk=data['job_id'])
    assert job.product == product
    assert job.normalized_article == normalize_part_code(product.article)
    delay.assert_called_once_with(job.pk)


@pytest.mark.django_db
def test_parse_endpoint_can_generate_after_enrichment(django_capture_on_commit_callbacks):
    tenant, api_key = make_tenant('parse-generate-api')
    product = make_product(tenant)
    client = Client()

    with patch('apps.products.tasks.parse_single_part_then_generate_description.delay') as delay:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                '/api/v1/products/parse/',
                {'product_id': product.pk, 'generate_after': True},
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )

    assert response.status_code == 201
    data = response.json()['data']
    job = tenant.product_parse_jobs.get(pk=data['job_id'])
    assert data['generate_after'] is True
    delay.assert_called_once_with(job.pk)


@pytest.mark.django_db
def test_regenerate_endpoint_uses_enrichment_pipeline(django_capture_on_commit_callbacks):
    tenant, api_key = make_tenant('regenerate-enriched-api')
    product = make_product(tenant)
    client = Client()

    with patch('apps.products.tasks.parse_single_part_then_generate_description.delay') as delay:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                f'/api/v1/products/{product.pk}/regenerate/',
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )

    assert response.status_code == 202
    data = response.json()['data']
    job = tenant.product_parse_jobs.get(pk=data['job_id'])
    assert job.product == product
    assert job.normalized_article == normalize_part_code(product.article)
    assert data['generate_after'] is True
    delay.assert_called_once_with(job.pk)


@pytest.mark.django_db
def test_regenerate_endpoint_rejects_other_tenant_product():
    tenant_a, api_key = make_tenant('regenerate-owner')
    tenant_b, _ = make_tenant('regenerate-other')
    product_b = make_product(tenant_b)
    client = Client()

    response = client.post(
        f'/api/v1/products/{product_b.pk}/regenerate/',
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
        {'product_id': product_b.pk},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 404
    assert tenant_a.product_parse_jobs.count() == 0


@pytest.mark.django_db
def test_parse_endpoint_rejects_non_auto_parts_tenant():
    tenant, api_key = make_tenant('parse-jewellery')
    tenant.catalog_domain = 'jewellery'
    tenant.save(update_fields=['catalog_domain'])
    product = make_product(tenant)
    client = Client()

    response = client.post(
        '/api/v1/products/parse/',
        {'product_id': product.pk},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'auto_parts_enrichment_disabled'
    assert tenant.product_parse_jobs.count() == 0


@pytest.mark.django_db
def test_regenerate_endpoint_for_non_auto_parts_tenant_queues_ai_without_enrichment(
    django_capture_on_commit_callbacks,
):
    tenant, api_key = make_tenant('regenerate-jewellery')
    tenant.catalog_domain = 'jewellery'
    tenant.save(update_fields=['catalog_domain'])
    product = make_product(tenant)
    client = Client()

    with patch('apps.ai_agent.tasks.generate_description_task.delay') as delay:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                f'/api/v1/products/{product.pk}/regenerate/',
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )

    assert response.status_code == 202
    data = response.json()['data']
    assert data['job_id'] is None
    assert data['state'] == 'queued'
    assert tenant.product_parse_jobs.count() == 0
    delay.assert_called_once_with(product.pk)


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
        {'product_id': product.pk},
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

    with patch('apps.ai_agent.tasks.generate_description_task.delay') as delay:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                f'/api/v1/products/{product.pk}/regenerate/',
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )

    assert response.status_code == 202
    assert response.json()['data']['job_id'] is None
    assert tenant.product_parse_jobs.count() == 0
    product.refresh_from_db()
    assert product.catalog_classification.domain == ProductCatalogClassification.Domain.JEWELLERY
    delay.assert_called_once_with(product.pk)


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
def test_bulk_action_endpoint_creates_throttled_tenant_job(django_capture_on_commit_callbacks):
    tenant, api_key = make_tenant('bulk-api')
    other_tenant, _ = make_tenant('bulk-other')
    product = make_product(tenant)
    other_product = make_product(other_tenant)
    client = Client()

    with patch('apps.products.tasks.process_bulk_product_action.delay') as delay:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                '/api/v1/products/bulk-actions/',
                {
                    'action': 'enrich_selected',
                    'product_ids': [product.pk, other_product.pk],
                    'batch_size': 1,
                    'pause_seconds': 30,
                },
                content_type='application/json',
                HTTP_AUTHORIZATION=f'Bearer {api_key}',
            )

    assert response.status_code == 201
    data = response.json()['data']
    job = tenant.product_bulk_action_jobs.get(pk=data['id'])
    assert job.product_ids == [product.pk]
    assert job.total_count == 1
    assert job.skipped_count == 1
    assert job.batch_size == 1
    assert job.pause_seconds == 30
    delay.assert_called_once_with(job.pk)
