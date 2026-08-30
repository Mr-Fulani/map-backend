from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils.timezone import now

from apps.core.models import BackgroundJobDispatch
from apps.products.models import (
    Product, ProductBulkActionJob, ProductCatalogClassification, TenantCatalogCategory,
)
from apps.products.services import (
    AutoPartsEnrichmentDisabled, ProductBulkActionService, ProductCategorySeedService,
)
from apps.tenants.models import CatalogDomain
from apps.tenants.services import TenantService


def make_tenant(slug, catalog_domain='auto_parts'):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    tenant.catalog_domain = catalog_domain
    tenant.save(update_fields=['catalog_domain'])
    from apps.products.services import ProductCategorySeedService
    ProductCategorySeedService.enable_tenant_catalog_domain(tenant, catalog_domain)
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
def test_bulk_action_processes_first_batch_without_redis_countdown(
    django_capture_on_commit_callbacks,
):
    tenant = make_tenant('bulk-process')
    products = [make_product(tenant, f'P{i}') for i in range(3)]
    job = ProductBulkActionService.create_job(
        tenant=tenant,
        action=ProductBulkActionJob.Action.ENRICH_SELECTED,
        product_ids=[product.pk for product in products],
        batch_size=2,
        pause_seconds=30,
    )

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            result = ProductBulkActionService.process_next_batch(job.pk)

    job.refresh_from_db()
    assert result['status'] == ProductBulkActionJob.Status.COOLING_DOWN
    assert job.processed_count == 2
    assert job.queued_count == 2
    assert job.next_batch_at is not None
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.products.tasks.parse_single_part',
    ).count() == 2
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.products.tasks.process_bulk_product_action',
        available_at=job.next_batch_at,
    ).count() == 1


@pytest.mark.django_db
def test_bulk_action_does_not_run_before_cooldown_deadline(
    django_capture_on_commit_callbacks,
):
    from datetime import timedelta

    tenant = make_tenant('bulk-cooldown-guard')
    product = make_product(tenant, 'P1')
    job = ProductBulkActionService.create_job(
        tenant=tenant,
        action=ProductBulkActionJob.Action.ENRICH_SELECTED,
        product_ids=[product.pk],
    )
    job.status = ProductBulkActionJob.Status.COOLING_DOWN
    job.next_batch_at = now() + timedelta(minutes=5)
    job.save(update_fields=['status', 'next_batch_at', 'updated_at'])

    with django_capture_on_commit_callbacks(execute=True):
        result = ProductBulkActionService.process_next_batch(job.pk)

    job.refresh_from_db()
    assert result['status'] == ProductBulkActionJob.Status.COOLING_DOWN
    assert job.processed_count == 0
    assert not BackgroundJobDispatch.objects.exists()


@pytest.mark.django_db
def test_dispatcher_publishes_pending_and_due_jobs_only(
    django_capture_on_commit_callbacks,
):
    from datetime import timedelta

    from apps.products.tasks import dispatch_due_product_bulk_jobs

    tenant = make_tenant('bulk-dispatch-due')
    pending = ProductBulkActionJob.objects.create(
        tenant=tenant,
        action=ProductBulkActionJob.Action.CLASSIFY_CATALOG_DOMAIN,
    )
    due = ProductBulkActionJob.objects.create(
        tenant=tenant,
        action=ProductBulkActionJob.Action.CLASSIFY_CATALOG_DOMAIN,
        status=ProductBulkActionJob.Status.COOLING_DOWN,
        next_batch_at=now() - timedelta(seconds=1),
    )
    future = ProductBulkActionJob.objects.create(
        tenant=tenant,
        action=ProductBulkActionJob.Action.CLASSIFY_CATALOG_DOMAIN,
        status=ProductBulkActionJob.Status.COOLING_DOWN,
        next_batch_at=now() + timedelta(minutes=5),
    )

    with patch('apps.core.tasks.execute_background_dispatch.apply_async') as publish:
        with django_capture_on_commit_callbacks(execute=True):
            result = dispatch_due_product_bulk_jobs()

    pending.refresh_from_db()
    due.refresh_from_db()
    future.refresh_from_db()
    assert result == {'selected': 2}
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.products.tasks.process_bulk_product_action',
    ).count() == 2
    assert publish.call_count == 2
    assert pending.last_dispatched_at is not None
    assert due.last_dispatched_at is not None
    assert future.last_dispatched_at is None


@pytest.mark.django_db
def test_dispatcher_keeps_durable_row_after_publish_failure(
    django_capture_on_commit_callbacks,
):
    from apps.products.tasks import dispatch_due_product_bulk_jobs

    tenant = make_tenant('bulk-dispatch-recovery')
    job = ProductBulkActionJob.objects.create(
        tenant=tenant,
        action=ProductBulkActionJob.Action.CLASSIFY_CATALOG_DOMAIN,
    )

    with patch(
        'apps.core.tasks.execute_background_dispatch.apply_async',
        side_effect=RuntimeError('broker unavailable'),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            result = dispatch_due_product_bulk_jobs()

    job.refresh_from_db()
    assert result == {'selected': 1}
    dispatch = BackgroundJobDispatch.objects.get()
    assert dispatch.status == BackgroundJobDispatch.Status.PENDING
    assert job.last_dispatched_at is not None


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

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            ProductBulkActionService.process_next_batch(job.pk)

    job.refresh_from_db()
    assert job.product_ids == [product.pk]
    assert job.skipped_count == 1
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.products.tasks.parse_single_part',
    ).count() == 1


@pytest.mark.django_db
def test_bulk_action_enrich_then_generate_uses_chained_task(django_capture_on_commit_callbacks):
    tenant = make_tenant('bulk-generate')
    product = make_product(tenant, 'P1')
    job = ProductBulkActionService.create_job(
        tenant=tenant,
        action=ProductBulkActionJob.Action.ENRICH_THEN_GENERATE,
        product_ids=[product.pk],
    )

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            ProductBulkActionService.process_next_batch(job.pk)

    assert not BackgroundJobDispatch.objects.filter(
        task_name='apps.products.tasks.parse_single_part',
    ).exists()
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.products.tasks.parse_single_part_then_generate_description',
    ).count() == 1


@pytest.mark.django_db
def test_bulk_generate_descriptions_uses_enrichment_aware_scheduler(django_capture_on_commit_callbacks):
    tenant = make_tenant('bulk-ai-enrichment-aware')
    product = make_product(tenant, 'P1')
    job = ProductBulkActionService.create_job(
        tenant=tenant,
        action=ProductBulkActionJob.Action.GENERATE_DESCRIPTIONS,
        product_ids=[product.pk],
    )

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            result = ProductBulkActionService.process_next_batch(job.pk)

    job.refresh_from_db()
    assert result['status'] == ProductBulkActionJob.Status.SUCCESS
    assert job.queued_count == 1
    assert tenant.product_parse_jobs.filter(product=product).count() == 1
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.products.tasks.parse_single_part_then_generate_description',
    ).count() == 1
    assert not BackgroundJobDispatch.objects.filter(
        task_name='apps.ai_agent.tasks.generate_description_task',
    ).exists()


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
def test_bulk_enrichment_skips_apparel_for_legacy_auto_tenant_with_two_domains():
    tenant = make_tenant('bulk-auto-plus-apparel')
    ProductCategorySeedService.enable_tenant_catalog_domain(
        tenant,
        'apparel',
        seed_templates=False,
    )
    apparel_category = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Куртки',
        root_domain=CatalogDomain.objects.get(slug='apparel'),
        domain=ProductCatalogClassification.Domain.APPAREL,
    )
    auto_part = make_product(tenant, 'P1', name='Колодки тормозные BREMBO P1')
    apparel = make_product(
        tenant,
        'JACKET1',
        brand='NO_BRAND',
        name='Мужская куртка',
        category_1c='Одежда',
    )
    apparel.catalog_category = apparel_category
    apparel.save(update_fields=['catalog_category', 'updated_at'])

    job = ProductBulkActionService.create_job(
        tenant=tenant,
        action=ProductBulkActionJob.Action.ENRICH_THEN_GENERATE,
        product_ids=[auto_part.pk, apparel.pk],
    )

    assert job.product_ids == [auto_part.pk]
    assert job.skipped_count == 1


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
    assert auto_part.catalog_category is not None
    assert auto_part.catalog_category.name == 'Тормозные колодки'
    assert auto_part.catalog_classification.domain == ProductCatalogClassification.Domain.AUTO_PARTS
    assert jewellery.catalog_classification.domain == ProductCatalogClassification.Domain.JEWELLERY


@pytest.mark.django_db
def test_bulk_find_images_queues_search_tasks(django_capture_on_commit_callbacks):
    tenant = make_tenant('bulk-find-images')
    products = [make_product(tenant, f'IMG{i}') for i in range(2)]
    job = ProductBulkActionService.create_job(
        tenant=tenant,
        action=ProductBulkActionJob.Action.FIND_IMAGES,
        product_ids=[product.pk for product in products],
        batch_size=20,
    )

    with patch('apps.core.tasks.execute_background_dispatch.apply_async'):
        with django_capture_on_commit_callbacks(execute=True):
            result = ProductBulkActionService.process_next_batch(job.pk)

    job.refresh_from_db()
    assert result['status'] == ProductBulkActionJob.Status.SUCCESS
    assert job.queued_count == 2
    assert job.skipped_count == 0
    assert BackgroundJobDispatch.objects.filter(
        task_name='apps.image_search.tasks.search_images_for_product',
    ).count() == 2
    from apps.image_search.models import ImageSearchTask
    assert ImageSearchTask.objects.filter(
        tenant=tenant,
        product__in=products,
    ).count() == 2
    assert all(
        task.dispatch.args == [task.product_id, task.pk]
        for task in ImageSearchTask.objects.filter(
            tenant=tenant,
            product__in=products,
        ).select_related('dispatch')
    )
