from importlib import import_module
import uuid

import pytest
from django.db import transaction
from django.db.migrations import AddIndex
from django.db.models.deletion import ProtectedError

from apps.image_search.models import (
    ImageSearchCache,
    ImageSearchIntent,
    ImageSearchLog,
    ImageSearchTask,
)


def test_image_search_retention_fields_have_global_indexes():
    assert {index.name for index in ImageSearchLog._meta.indexes} >= {
        'img_log_created_idx',
    }
    assert {index.name for index in ImageSearchTask._meta.indexes} >= {
        'img_task_created_idx',
    }
    assert {index.name for index in ImageSearchCache._meta.indexes} >= {
        'img_cache_expires_idx',
    }
    assert {index.name for index in ImageSearchIntent._meta.indexes} >= {
        'img_intent_tenant_created_idx',
    }


def test_retention_index_migration_follows_durable_dispatch_migration():
    migration = import_module(
        'apps.image_search.migrations.0007_retention_indexes',
    ).Migration

    assert migration.dependencies == [
        ('image_search', '0006_imagesearchtask_dispatch'),
    ]
    assert all(isinstance(operation, AddIndex) for operation in migration.operations)
    assert {operation.index.name for operation in migration.operations} == {
        'img_log_created_idx',
        'img_task_created_idx',
        'img_cache_expires_idx',
    }


@pytest.mark.django_db
def test_active_paid_workflow_protects_image_task_and_intent_from_delete():
    from apps.products.models import Product
    from apps.tenants.services import TenantService
    from apps.web_research.models import WebSearchWorkflow

    tenant, _ = TenantService.create_tenant(
        'Image delete guard',
        'image-delete-guard',
        'image-delete-guard@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='IMAGE-DELETE-GUARD',
        name='Image delete guard',
        price='1.00',
    )
    intent = ImageSearchIntent.objects.create(
        tenant=tenant,
        operation=ImageSearchIntent.Operation.SINGLE,
        idempotency_key=uuid.uuid4(),
        request_fingerprint='a' * 64,
        request_payload={'product_id': product.pk},
    )
    task = ImageSearchTask.objects.create(
        tenant=tenant,
        product=product,
        intent=intent,
        task_id='active-image-delete-guard',
    )
    workflow = WebSearchWorkflow.objects.create(
        tenant=tenant,
        product=product,
        operation='image_search',
        domain_reference=f'product:{tenant.pk}:{product.pk}',
        workflow_key=f'image-search-task:{task.pk}',
        input_fingerprint='b' * 64,
        input_snapshot={'version': 1},
        status=WebSearchWorkflow.Status.APPLY_PENDING,
    )

    with pytest.raises(ProtectedError, match='active paid provider workflow'), \
         transaction.atomic():
        task.delete()
    with pytest.raises(ProtectedError, match='active paid provider workflow'), \
         transaction.atomic():
        ImageSearchTask.objects.filter(pk=task.pk).delete()
    with pytest.raises(ProtectedError, match='active paid provider workflow'), \
         transaction.atomic():
        intent.delete()
    with pytest.raises(ProtectedError, match='active paid provider workflow'), \
         transaction.atomic():
        ImageSearchIntent.objects.filter(pk=intent.pk).delete()

    workflow.status = WebSearchWorkflow.Status.APPLIED
    workflow.product = None
    workflow.save(update_fields=['status', 'product', 'updated_at'])
    intent.delete()

    assert not ImageSearchIntent.objects.filter(pk=intent.pk).exists()
    assert not ImageSearchTask.objects.filter(pk=task.pk).exists()
