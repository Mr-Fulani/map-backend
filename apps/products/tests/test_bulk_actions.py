from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.products.models import Product, ProductBulkActionJob, ProductCatalogClassification
from apps.products.services import AutoPartsEnrichmentDisabled, ProductBulkActionService
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant


def make_product(tenant, article, brand='BREMBO', name=None, category_1c=''):
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
def test_bulk_action_processes_first_batch_and_schedules_next(django_capture_on_commit_callbacks):
    tenant = make_tenant('bulk-process')
    products = [make_product(tenant, f'P{i}') for i in range(3)]
    job = ProductBulkActionService.create_job(
        tenant=tenant,
        action=ProductBulkActionJob.Action.ENRICH_SELECTED,
        product_ids=[product.pk for product in products],
        batch_size=2,
        pause_seconds=30,
    )

    with patch('apps.products.tasks.parse_single_part.delay') as parse_delay:
        with patch('apps.products.tasks.process_bulk_product_action.apply_async') as bulk_delay:
            with django_capture_on_commit_callbacks(execute=True):
                result = ProductBulkActionService.process_next_batch(job.pk)

    job.refresh_from_db()
    assert result['status'] == ProductBulkActionJob.Status.COOLING_DOWN
    assert job.processed_count == 2
    assert job.queued_count == 2
    assert job.next_batch_at is not None
    assert parse_delay.call_count == 2
    bulk_delay.assert_called_once()


@pytest.mark.django_db
def test_bulk_action_does_not_process_other_tenant_products(django_capture_on_commit_callbacks):
    tenant = make_tenant('bulk-safe')
    other_tenant = make_tenant('bulk-safe-other')
    product = make_product(tenant, 'P1')
    other_product = make_product(other_tenant, 'P2')
    job = ProductBulkActionService.create_job(
        tenant=tenant,
        action=ProductBulkActionJob.Action.ENRICH_SELECTED,
        product_ids=[product.pk, other_product.pk],
        batch_size=20,
    )

    with patch('apps.products.tasks.parse_single_part.delay') as parse_delay:
        with django_capture_on_commit_callbacks(execute=True):
            ProductBulkActionService.process_next_batch(job.pk)

    job.refresh_from_db()
    assert job.product_ids == [product.pk]
    assert job.skipped_count == 1
    assert parse_delay.call_count == 1


@pytest.mark.django_db
def test_bulk_action_enrich_then_generate_uses_chained_task(django_capture_on_commit_callbacks):
    tenant = make_tenant('bulk-generate')
    product = make_product(tenant, 'P1')
    job = ProductBulkActionService.create_job(
        tenant=tenant,
        action=ProductBulkActionJob.Action.ENRICH_THEN_GENERATE,
        product_ids=[product.pk],
    )

    with patch('apps.products.tasks.parse_single_part.delay') as parse_delay:
        with patch('apps.products.tasks.parse_single_part_then_generate_description.delay') as chained_delay:
            with django_capture_on_commit_callbacks(execute=True):
                ProductBulkActionService.process_next_batch(job.pk)

    parse_delay.assert_not_called()
    chained_delay.assert_called_once()


@pytest.mark.django_db
def test_bulk_generate_descriptions_uses_enrichment_aware_scheduler(django_capture_on_commit_callbacks):
    tenant = make_tenant('bulk-ai-enrichment-aware')
    product = make_product(tenant, 'P1')
    job = ProductBulkActionService.create_job(
        tenant=tenant,
        action=ProductBulkActionJob.Action.GENERATE_DESCRIPTIONS,
        product_ids=[product.pk],
    )

    with patch('apps.products.tasks.parse_single_part_then_generate_description.delay') as chained_delay:
        with patch('apps.ai_agent.tasks.generate_description_task.delay') as ai_delay:
            with django_capture_on_commit_callbacks(execute=True):
                result = ProductBulkActionService.process_next_batch(job.pk)

    job.refresh_from_db()
    assert result['status'] == ProductBulkActionJob.Status.SUCCESS
    assert job.queued_count == 1
    assert tenant.product_parse_jobs.filter(product=product).count() == 1
    chained_delay.assert_called_once()
    ai_delay.assert_not_called()


@pytest.mark.django_db
def test_bulk_enrichment_rejects_non_auto_parts_tenant():
    tenant = make_tenant('bulk-jewellery')
    tenant.catalog_domain = 'jewellery'
    tenant.save(update_fields=['catalog_domain'])
    product = make_product(tenant, 'P1')

    with pytest.raises(AutoPartsEnrichmentDisabled) as exc:
        ProductBulkActionService.create_job(
            tenant=tenant,
            action=ProductBulkActionJob.Action.ENRICH_THEN_GENERATE,
            product_ids=[product.pk],
        )

    assert 'Автозапчастное обогащение' in str(exc.value)


@pytest.mark.django_db
def test_bulk_enrichment_skips_non_auto_parts_products_for_mixed_tenant():
    tenant = make_tenant('bulk-mixed')
    tenant.catalog_domain = 'mixed'
    tenant.save(update_fields=['catalog_domain'])
    auto_part = make_product(tenant, 'P1', name='Колодки тормозные BREMBO P1')
    jewellery = make_product(
        tenant, 'RING1', brand='NO_BRAND', name='Золотое кольцо', category_1c='Украшения',
    )

    job = ProductBulkActionService.create_job(
        tenant=tenant,
        action=ProductBulkActionJob.Action.ENRICH_THEN_GENERATE,
        product_ids=[auto_part.pk, jewellery.pk],
    )

    assert job.product_ids == [auto_part.pk]
    assert job.skipped_count == 1
    classification = auto_part.catalog_classification
    assert classification.domain == ProductCatalogClassification.Domain.AUTO_PARTS
    assert classification.reason


@pytest.mark.django_db
def test_bulk_classification_action_classifies_products(django_capture_on_commit_callbacks):
    tenant = make_tenant('bulk-classify')
    auto_part = make_product(tenant, 'P1', name='Колодки тормозные BREMBO P1')
    jewellery = make_product(
        tenant, 'RING1', brand='NO_BRAND', name='Золотое кольцо', category_1c='Украшения',
    )
    job = ProductBulkActionService.create_job(
        tenant=tenant,
        action=ProductBulkActionJob.Action.CLASSIFY_CATALOG_DOMAIN,
        product_ids=[auto_part.pk, jewellery.pk],
        batch_size=20,
    )

    with django_capture_on_commit_callbacks(execute=True):
        result = ProductBulkActionService.process_next_batch(job.pk)

    job.refresh_from_db()
    auto_part.refresh_from_db()
    jewellery.refresh_from_db()
    assert result['status'] == ProductBulkActionJob.Status.SUCCESS
    assert job.success_count == 2
    assert job.queued_count == 2
    assert auto_part.catalog_classification.domain == ProductCatalogClassification.Domain.AUTO_PARTS
    assert jewellery.catalog_classification.domain == ProductCatalogClassification.Domain.JEWELLERY
