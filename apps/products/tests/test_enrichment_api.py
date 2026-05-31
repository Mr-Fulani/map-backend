from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.products.enrichment import normalize_part_code
from apps.products.models import (
    Product, ProductCatalogClassification, ProductCrossCode, ReviewStatus, VehicleFitment,
    TenantCatalogCategory, TenantCategoryMapping,
)
from apps.products.services import ProductEnrichmentService
from apps.tenants.models import CatalogDomain
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
def test_fitment_review_rejects_and_refreshes_product_applicability():
    tenant, api_key = make_tenant('fitment-review-api')
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

    response = Client().post(
        f'/api/v1/products/{product.pk}/fitments/{fitment.pk}/reject/',
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 200
    fitment.refresh_from_db()
    product.refresh_from_db()
    assert fitment.review_status == ReviewStatus.REJECTED
    assert fitment.needs_review is False
    assert product.applicability == []


@pytest.mark.django_db
def test_fitment_review_rejects_other_tenant_record():
    tenant_a, api_key = make_tenant('fitment-review-owner')
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

    response = Client().post(
        f'/api/v1/products/{product_a.pk}/fitments/{fitment_b.pk}/approve/',
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 404
    fitment_b.refresh_from_db()
    assert fitment_b.review_status == ReviewStatus.PENDING


@pytest.mark.django_db
def test_catalog_classification_review_approve_marks_manual():
    tenant, api_key = make_tenant('classification-review-api')
    product = make_product(tenant, name='Колодки тормозные BREMBO')
    classification = ProductEnrichmentService.classify_product_catalog_domain(product)
    classification.needs_review = True
    classification.save(update_fields=['needs_review', 'updated_at'])

    response = Client().post(
        f'/api/v1/products/{product.pk}/catalog-classification/approve/',
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 200
    classification.refresh_from_db()
    assert classification.review_status == ReviewStatus.APPROVED
    assert classification.needs_review is False
    assert classification.source == ProductCatalogClassification.Source.MANUAL


@pytest.mark.django_db
def test_catalog_classification_review_cannot_approve_unknown_domain():
    tenant, api_key = make_tenant('classification-review-unknown')
    product = make_product(
        tenant,
        article='ITEM1',
        brand='NO_BRAND',
        name='Товар без понятной категории',
    )
    classification = ProductEnrichmentService.classify_product_catalog_domain(product)

    response = Client().post(
        f'/api/v1/products/{product.pk}/catalog-classification/approve/',
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'unknown_classification'
    classification.refresh_from_db()
    assert classification.review_status == ReviewStatus.PENDING


@pytest.mark.django_db
def test_assign_catalog_category_reclassifies_previous_manual_unknown_classification():
    tenant, api_key = make_tenant('classification-force-after-category')
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

    response = Client().post(
        '/api/v1/products/catalog-categories/assign/',
        {'product_ids': [product.pk], 'catalog_category': category.pk},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 200
    classification.refresh_from_db()
    assert classification.domain == ProductCatalogClassification.Domain.AUTO_PARTS
    assert classification.source == ProductCatalogClassification.Source.RULES
    assert classification.review_status == ReviewStatus.PENDING


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
    tenant, api_key = make_tenant('catalog-domains-api')
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
    client = Client()

    response = client.get(
        '/api/v1/catalog-domains/',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 200
    data = response.json()['data']
    slugs = [item['slug'] for item in data]
    assert 'custom_goods' in slugs
    assert 'electronics' in slugs
    assert 'hidden' not in slugs
    custom_goods = next(item for item in data if item['slug'] == 'custom_goods')
    assert custom_goods['seo_title'] == 'Спецтовары купить'


@pytest.mark.django_db
def test_enabling_catalog_domain_seeds_tenant_categories():
    tenant, api_key = make_tenant('catalog-domain-enable')
    client = Client()

    response = client.post(
        '/api/v1/catalog-domains/',
        {'domain_slug': 'electronics', 'is_enabled': True},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
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

    image = SimpleUploadedFile('fallback.jpg', b'image-bytes', content_type='image/jpeg')
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
