from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.products.models import Product, ProductBulkActionJob
from apps.products.services import ProductBulkActionService
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant


def make_product(tenant, article):
    return Product.objects.create(
        tenant=tenant,
        article=article,
        brand='BREMBO',
        name=f'BREMBO {article}',
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
