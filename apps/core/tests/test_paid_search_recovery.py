from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.core.models import BackgroundJobDispatch
from apps.core.paid_search_recovery import (
    PaidSearchCheckpointRecoveryError,
    resume_image_search_checkpoint,
)
from apps.image_search.models import ImageSearchTask
from apps.products.models import Product
from apps.tenants.services import TenantService
from apps.web_research.models import WebSearchWorkflow


def _failed_image_owner(slug: str, *, workflow_status: str, dispatch_status: str):
    tenant, _ = TenantService.create_tenant(
        f'Checkpoint recovery {slug}',
        slug,
        f'{slug}@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article=f'RECOVERY-{slug}',
        name='Recovery owner',
        price='1.00',
    )
    tracking = ImageSearchTask.objects.create(
        tenant=tenant,
        product=product,
        task_id=f'recovery-{slug}',
        status=ImageSearchTask.Status.FAILED,
    )
    dispatch = BackgroundJobDispatch.objects.create(
        task_name='apps.image_search.tasks.search_images_for_product',
        queue='image_search',
        args=[product.pk, tracking.pk],
        deduplication_key=f'image-search-request:{tracking.task_id}',
        status=dispatch_status,
        max_run_attempts=5,
        run_attempts=5,
    )
    tracking.dispatch = dispatch
    tracking.save(update_fields=['dispatch', 'updated_at'])
    workflow = WebSearchWorkflow.objects.create(
        tenant=tenant,
        product=product,
        operation='image_search',
        domain_reference=f'product:{tenant.pk}:{product.pk}:image_search',
        workflow_key=f'image-search-task:{tracking.pk}',
        status=workflow_status,
        input_fingerprint='a' * 64,
        input_snapshot={'version': 1},
    )
    return tracking, dispatch, workflow


@pytest.mark.django_db
def test_checkpoint_recovery_refuses_uncertain_provider_outcome():
    tracking, dispatch, _ = _failed_image_owner(
        'recovery-refuses-uncertain',
        workflow_status=WebSearchWorkflow.Status.UNCERTAIN,
        dispatch_status=BackgroundJobDispatch.Status.FAILED,
    )

    with pytest.raises(
        PaidSearchCheckpointRecoveryError,
        match='not waiting for safe local replay',
    ):
        resume_image_search_checkpoint(tracking.pk)

    tracking.refresh_from_db()
    dispatch.refresh_from_db()
    assert tracking.status == ImageSearchTask.Status.FAILED
    assert dispatch.status == BackgroundJobDispatch.Status.FAILED


@pytest.mark.django_db
def test_checkpoint_recovery_never_revives_successful_dispatch():
    tracking, dispatch, _ = _failed_image_owner(
        'recovery-refuses-success',
        workflow_status=WebSearchWorkflow.Status.IN_PROGRESS,
        dispatch_status=BackgroundJobDispatch.Status.SUCCEEDED,
    )

    with pytest.raises(
        PaidSearchCheckpointRecoveryError,
        match='not an exhausted recoverable delivery',
    ):
        resume_image_search_checkpoint(tracking.pk)

    tracking.refresh_from_db()
    dispatch.refresh_from_db()
    assert tracking.status == ImageSearchTask.Status.FAILED
    assert dispatch.status == BackgroundJobDispatch.Status.SUCCEEDED


def test_checkpoint_recovery_command_requires_exact_confirmation():
    with patch(
        'apps.core.management.commands.resume_paid_search_checkpoint.'
        'resume_image_search_checkpoint',
    ) as recovery, pytest.raises(CommandError, match='must exactly equal'):
        call_command(
            'resume_paid_search_checkpoint',
            image_task_id=123,
            confirm='123',
        )

    recovery.assert_not_called()
