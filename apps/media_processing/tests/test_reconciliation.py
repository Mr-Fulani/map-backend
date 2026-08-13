from datetime import timedelta
from decimal import Decimal
from io import StringIO
import uuid

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import RequestFactory
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from django_celery_beat.models import PeriodicTask

from apps.billing.ai_wallet import AIWalletService
from apps.media_processing.admin import MediaProcessingJobAdmin
from apps.media_processing.models import MediaProcessingJob, MediaProviderPolicy
from apps.media_processing.providers.base import (
    MediaOperation,
    MediaProviderResult,
    MediaProviderResultStatus,
)
from apps.media_processing.providers.registry import register_media_provider
from apps.media_processing.providers.registry import clear_media_provider_registry
from apps.media_processing.services import (
    MediaProviderOutcomeUncertain, _checkpoint_provider_result,
    _claim_provider_checkpoint_for_apply, _release_job_credits,
    _reserve_job_credits, _settle_job_credits, create_processing_job, submit_job,
)
from apps.media_processing.tests.test_services import FakeExternalProvider
from apps.media_processing.tasks import (
    process_media_job,
    resume_stale_media_provider_checkpoints,
)
from apps.products.models import Product, ProductImage
from apps.tenants.services import TenantService


@pytest.fixture(autouse=True)
def isolated_registry():
    clear_media_provider_registry()
    yield
    clear_media_provider_registry()


@pytest.fixture
def product_image(db):
    tenant, _ = TenantService.create_tenant(
        'Media reconciliation tenant',
        'media-reconciliation-tenant',
        'media-reconciliation@test.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='MEDIA-RECONCILE-1',
        brand='TEST',
        name='Media reconciliation product',
        price='100.00',
    )
    return ProductImage.objects.create(
        product=product,
        s3_key='products/media/reconciliation-source.jpg',
        sha256='media-reconciliation-source',
        status=ProductImage.Status.MANUALLY_SET,
    )


def create_test_job(product_image, *, idempotency_key=''):
    try:
        register_media_provider(FakeExternalProvider)
    except ValueError as exc:
        if 'already registered' not in str(exc):
            raise
    MediaProviderPolicy.objects.get_or_create(
        provider_id=FakeExternalProvider.provider_id,
        defaults={
            'display_name': 'Fake external',
            'capabilities': [MediaOperation.RESIZE.value],
            'operation_credit_costs': {MediaOperation.RESIZE.value: '0'},
        },
    )
    return create_processing_job(
        product_image=product_image,
        operations=['resize'],
        idempotency_key=idempotency_key,
    )


@pytest.mark.django_db
def test_unresolved_media_job_cannot_be_deleted_directly(product_image):
    job = create_test_job(product_image)

    with pytest.raises(ProtectedError, match='media job'):
        with transaction.atomic():
            job.delete()

    assert MediaProcessingJob.objects.filter(pk=job.pk).exists()


@pytest.mark.django_db
def test_reconciled_terminal_media_job_can_be_deleted(product_image):
    job = create_test_job(product_image)
    job.status = MediaProcessingJob.Status.FAILED
    job.error_code = 'pre_provider_validation_failed'
    job.save(update_fields=['status', 'error_code', 'updated_at'])

    deleted_pk = job.pk
    job.delete()

    assert MediaProcessingJob.objects.filter(pk=deleted_pk).exists() is False


@pytest.mark.django_db
def test_admin_hides_delete_for_unresolved_media_job(product_image):
    job = create_test_job(product_image)
    resolved = create_test_job(product_image, idempotency_key=uuid.uuid4())
    resolved.status = MediaProcessingJob.Status.FAILED
    resolved.error_code = 'pre_provider_validation_failed'
    resolved.save(update_fields=['status', 'error_code', 'updated_at'])
    user = get_user_model().objects.create_superuser(
        email='media-admin@test.com',
        password='pass12345',
    )
    request = RequestFactory().get('/admin/media_processing/mediaprocessingjob/')
    request.user = user
    model_admin = MediaProcessingJobAdmin(MediaProcessingJob, admin.site)

    assert model_admin.has_delete_permission(request, obj=None) is False
    assert model_admin.has_delete_permission(request, obj=job) is False
    assert model_admin.has_delete_permission(request, obj=resolved) is True


@pytest.mark.django_db
@pytest.mark.parametrize('action,expected_reserved', [
    ('release', Decimal('0')),
    ('settle-reserved', Decimal('0')),
])
def test_operator_reconciles_exactly_one_uncertain_media_job(
    product_image,
    isolated_registry,
    action,
    expected_reserved,
):
    register_media_provider(FakeExternalProvider)
    policy = MediaProviderPolicy.objects.create(
        provider_id=FakeExternalProvider.provider_id,
        display_name='Fake external',
        capabilities=[MediaOperation.RESIZE.value],
        operation_credit_costs={MediaOperation.RESIZE.value: '2'},
    )
    job = create_processing_job(product_image=product_image, operations=['resize'])
    job.provider_id = policy.provider_id
    _reserve_job_credits(job, Decimal('2'))
    job.status = MediaProcessingJob.Status.FAILED
    job.error_code = 'outcome_uncertain'
    job.save(update_fields=['provider_id', 'status', 'error_code', 'updated_at'])

    call_command(
        'reconcile_media_provider_outcome',
        job_id=job.pk,
        confirm_job_id=job.pk,
        action=action,
        note='Checked provider dashboard reference manually.',
    )
    # Same action is an idempotent audit replay, never a second wallet mutation.
    call_command(
        'reconcile_media_provider_outcome',
        job_id=job.pk,
        confirm_job_id=job.pk,
        action=action,
        note='Checked provider dashboard reference manually.',
    )

    job.refresh_from_db()
    wallet = AIWalletService.summary(job.tenant)
    assert wallet['reserved'] == expected_reserved
    assert job.provider_metadata['reconciliation']['action'] == action
    assert job.error_code.startswith('outcome_reconciled_')


@pytest.mark.django_db
def test_media_reconciliation_rejects_non_uncertain_job(
    product_image,
    isolated_registry,
):
    register_media_provider(FakeExternalProvider)
    MediaProviderPolicy.objects.create(
        provider_id=FakeExternalProvider.provider_id,
        display_name='Fake external',
        capabilities=[MediaOperation.RESIZE.value],
        operation_credit_costs={MediaOperation.RESIZE.value: '0'},
    )
    job = create_processing_job(product_image=product_image, operations=['resize'])
    with pytest.raises(CommandError, match='not awaiting'):
        call_command(
            'reconcile_media_provider_outcome',
            job_id=job.pk,
            confirm_job_id=job.pk,
            action='release',
            note='Checked provider dashboard reference manually.',
        )


@pytest.mark.django_db
def test_media_reconciliation_rejects_wallet_action_that_contradicts_audit(
    product_image,
    isolated_registry,
):
    register_media_provider(FakeExternalProvider)
    policy = MediaProviderPolicy.objects.create(
        provider_id=FakeExternalProvider.provider_id,
        display_name='Fake external',
        capabilities=[MediaOperation.RESIZE.value],
        operation_credit_costs={MediaOperation.RESIZE.value: '2'},
    )
    job = create_processing_job(product_image=product_image, operations=['resize'])
    job.provider_id = policy.provider_id
    _reserve_job_credits(job, Decimal('2'))
    _settle_job_credits(job)
    job.status = MediaProcessingJob.Status.FAILED
    job.error_code = 'outcome_uncertain'
    job.save(update_fields=[
        'provider_id', 'provider_metadata', 'charged_credits', 'status',
        'error_code', 'updated_at',
    ])

    with pytest.raises(CommandError, match='already settled'):
        call_command(
            'reconcile_media_provider_outcome',
            job_id=job.pk,
            confirm_job_id=job.pk,
            action='release',
            note='Checked provider dashboard reference manually.',
        )


@pytest.mark.django_db
def test_operator_applies_known_checkpoint_idempotently_without_provider_call(
    product_image,
    isolated_registry,
):
    register_media_provider(FakeExternalProvider)
    policy = MediaProviderPolicy.objects.create(
        provider_id=FakeExternalProvider.provider_id,
        display_name='Known provider',
        capabilities=[MediaOperation.RESIZE.value],
        operation_credit_costs={MediaOperation.RESIZE.value: '2'},
    )
    job = create_processing_job(
        product_image=product_image,
        operations=['resize'],
        provider_id=policy.provider_id,
    )
    _reserve_job_credits(job, Decimal('2'))
    _checkpoint_provider_result(job, MediaProviderResult(
        status=MediaProviderResultStatus.PENDING,
        provider_job_id='known-remote-job',
    ))
    job.status = MediaProcessingJob.Status.FAILED
    job.error_code = 'outcome_uncertain'
    job.save(update_fields=['status', 'error_code', 'updated_at'])

    for _ in range(2):
        call_command(
            'reconcile_media_provider_outcome',
            job_id=job.pk,
            confirm_job_id=job.pk,
            action='apply-known',
            note='Apply the durable provider response checkpoint.',
        )

    job.refresh_from_db()
    assert job.status == MediaProcessingJob.Status.SUBMITTED
    assert job.provider_job_id == 'known-remote-job'
    assert (
        job.provider_response_state
        == MediaProcessingJob.ProviderResponseState.APPLIED
    )
    assert job.provider_response_enc is None
    assert job.provider_metadata['reconciliation']['action'] == 'apply-known'
    assert AIWalletService.summary(job.tenant)['reserved'] == Decimal('0')


@pytest.mark.django_db
def test_known_accepted_response_rejects_release_but_allows_explicit_settlement(
    product_image,
    isolated_registry,
):
    register_media_provider(FakeExternalProvider)
    policy = MediaProviderPolicy.objects.create(
        provider_id=FakeExternalProvider.provider_id,
        display_name='Known accounting provider',
        capabilities=[MediaOperation.RESIZE.value],
        operation_credit_costs={MediaOperation.RESIZE.value: '2'},
    )
    job = create_processing_job(
        product_image=product_image,
        operations=['resize'],
        provider_id=policy.provider_id,
    )
    _reserve_job_credits(job, Decimal('2'))
    _checkpoint_provider_result(job, MediaProviderResult(
        status=MediaProviderResultStatus.PENDING,
        provider_job_id='known-accepted-job',
    ))
    job.status = MediaProcessingJob.Status.FAILED
    job.error_code = 'outcome_uncertain'
    job.save(update_fields=['status', 'error_code', 'updated_at'])

    with pytest.raises(CommandError, match='release is unsafe'):
        call_command(
            'reconcile_media_provider_outcome',
            job_id=job.pk,
            confirm_job_id=job.pk,
            action='release',
            note='Checked the durable accepted provider response.',
        )
    call_command(
        'reconcile_media_provider_outcome',
        job_id=job.pk,
        confirm_job_id=job.pk,
        action='settle-reserved',
        note='Accept provider charge and abandon unusable local apply.',
    )

    job.refresh_from_db()
    assert (
        job.provider_response_state
        == MediaProcessingJob.ProviderResponseState.ACCOUNTING_RESOLVED
    )
    assert job.provider_response_enc is None
    assert AIWalletService.summary(job.tenant)['reserved'] == Decimal('0')


@pytest.mark.django_db
def test_apply_claim_excludes_accounting_and_stale_claim_resumes_without_provider(
    product_image,
    isolated_registry,
):
    register_media_provider(FakeExternalProvider)
    policy = MediaProviderPolicy.objects.create(
        provider_id=FakeExternalProvider.provider_id,
        display_name='Claim race provider',
        capabilities=[MediaOperation.RESIZE.value],
        operation_credit_costs={MediaOperation.RESIZE.value: '2'},
    )
    job = create_processing_job(
        product_image=product_image,
        operations=['resize'],
        provider_id=policy.provider_id,
    )
    _reserve_job_credits(job, Decimal('2'))
    _checkpoint_provider_result(job, FakeExternalProvider().process(None))
    job.status = MediaProcessingJob.Status.FAILED
    job.error_code = 'outcome_uncertain'
    job.save(update_fields=['status', 'error_code', 'updated_at'])

    _checkpoint, apply_token = _claim_provider_checkpoint_for_apply(job)
    assert apply_token is not None
    for action in ('release', 'settle-reserved'):
        with pytest.raises(CommandError, match='being applied'):
            call_command(
                'reconcile_media_provider_outcome',
                job_id=job.pk,
                confirm_job_id=job.pk,
                action=action,
                note='Competing accounting action must lose to apply claim.',
            )
    assert AIWalletService.summary(job.tenant)['reserved'] == Decimal('2')

    MediaProcessingJob.objects.filter(pk=job.pk).update(
        provider_response_apply_claimed_at=timezone.now() - timedelta(minutes=11),
    )
    from unittest.mock import patch
    with (
        patch.object(
            FakeExternalProvider,
            'process',
            side_effect=AssertionError('stale resume must not call provider'),
        ) as provider_call,
        patch(
            'apps.media_processing.services.default_storage.save',
            return_value='products/media/stale-resume.png',
        ),
    ):
        submit_job(job)

    job.refresh_from_db()
    assert job.status == MediaProcessingJob.Status.SUCCEEDED
    assert (
        job.provider_response_state
        == MediaProcessingJob.ProviderResponseState.APPLIED
    )
    assert AIWalletService.summary(job.tenant)['reserved'] == Decimal('0')
    provider_call.assert_not_called()


@pytest.mark.django_db
def test_known_accepted_checkpoint_fails_closed_after_reservation_release(
    product_image,
    isolated_registry,
):
    register_media_provider(FakeExternalProvider)
    policy = MediaProviderPolicy.objects.create(
        provider_id=FakeExternalProvider.provider_id,
        display_name='Contradictory accounting provider',
        capabilities=[MediaOperation.RESIZE.value],
        operation_credit_costs={MediaOperation.RESIZE.value: '2'},
    )
    job = create_processing_job(
        product_image=product_image,
        operations=['resize'],
        provider_id=policy.provider_id,
    )
    _reserve_job_credits(job, Decimal('2'))
    _checkpoint_provider_result(job, FakeExternalProvider().process(None))
    _release_job_credits(job, reason='simulated_contradictory_operator_release')

    with pytest.raises(MediaProviderOutcomeUncertain, match='противоречит'):
        submit_job(job)

    job.refresh_from_db()
    assert job.status != MediaProcessingJob.Status.SUCCEEDED
    assert (
        job.provider_response_state
        == MediaProcessingJob.ProviderResponseState.RECORDED
    )
    assert not job.variants.exists()


@pytest.mark.django_db
def test_periodic_recovery_durably_enqueues_only_stale_applying_checkpoint(
    product_image,
):
    job = MediaProcessingJob.objects.create(
        tenant=product_image.product.tenant,
        product_image=product_image,
        operations=['resize'],
    )
    _checkpoint_provider_result(job, MediaProviderResult(
        status=MediaProviderResultStatus.PENDING,
        provider_job_id='stale-periodic-job',
    ))
    _claim_provider_checkpoint_for_apply(job)
    MediaProcessingJob.objects.filter(pk=job.pk).update(
        provider_response_apply_claimed_at=timezone.now() - timedelta(minutes=11),
    )

    from unittest.mock import patch
    with patch(
        'apps.core.dispatch.enqueue_durable_task',
    ) as enqueue:
        result = resume_stale_media_provider_checkpoints.run()

    assert result == {'stale_checkpoints_enqueued': 1}
    enqueue.assert_called_once_with(
        'apps.media_processing.tasks.process_media_job',
        args=[job.pk],
        deduplication_key=(
            f'media-checkpoint-resume:{job.pk}:{job.provider_response_digest}:'
            f'{job.provider_response_apply_token}'
        ),
        max_run_attempts=4,
        revive_failed=True,
    )


@pytest.mark.django_db
def test_repeated_stale_claim_cycles_get_distinct_recovery_dispatch_keys(
    product_image,
):
    job = MediaProcessingJob.objects.create(
        tenant=product_image.product.tenant,
        product_image=product_image,
        operations=['resize'],
    )
    _checkpoint_provider_result(job, MediaProviderResult(
        status=MediaProviderResultStatus.PENDING,
        provider_job_id='multi-crash-job',
    ))
    _claim_provider_checkpoint_for_apply(job)
    MediaProcessingJob.objects.filter(pk=job.pk).update(
        provider_response_apply_claimed_at=timezone.now() - timedelta(minutes=11),
    )

    from unittest.mock import patch
    with patch('apps.core.dispatch.enqueue_durable_task') as enqueue:
        resume_stale_media_provider_checkpoints.run()
        first_key = enqueue.call_args.kwargs['deduplication_key']

        second_token = uuid.uuid4()
        MediaProcessingJob.objects.filter(pk=job.pk).update(
            provider_response_apply_token=second_token,
            provider_response_apply_claimed_at=(
                timezone.now() - timedelta(minutes=11)
            ),
        )
        resume_stale_media_provider_checkpoints.run()
        second_key = enqueue.call_args.kwargs['deduplication_key']

    assert enqueue.call_count == 2
    assert first_key != second_key
    assert first_key.endswith(str(job.provider_response_apply_token))
    assert second_key.endswith(str(second_token))


@pytest.mark.django_db
def test_stale_apply_loser_cannot_overwrite_winner_success(product_image):
    job = MediaProcessingJob.objects.create(
        tenant=product_image.product.tenant,
        product_image=product_image,
        operations=['resize'],
        status=MediaProcessingJob.Status.FAILED,
        error_code='outcome_uncertain',
        provider_response_state=MediaProcessingJob.ProviderResponseState.RECORDED,
    )

    def winner_commits_then_loser_observes_token_loss(stale_job):
        MediaProcessingJob.objects.filter(pk=stale_job.pk).update(
            status=MediaProcessingJob.Status.SUCCEEDED,
            error_code='',
            error_message='',
            finished_at=timezone.now(),
            provider_response_state=(
                MediaProcessingJob.ProviderResponseState.APPLIED
            ),
            provider_response_apply_token=None,
            provider_response_apply_claimed_at=None,
            provider_response_resolved_at=timezone.now(),
        )
        raise MediaProviderOutcomeUncertain('stale apply token lost')

    from unittest.mock import patch
    with patch(
        'apps.media_processing.services.submit_job',
        side_effect=winner_commits_then_loser_observes_token_loss,
    ):
        result = process_media_job.run(job.pk)

    job.refresh_from_db()
    assert result['status'] == MediaProcessingJob.Status.SUCCEEDED
    assert job.status == MediaProcessingJob.Status.SUCCEEDED
    assert job.error_code == ''
    assert (
        job.provider_response_state
        == MediaProcessingJob.ProviderResponseState.APPLIED
    )


@pytest.mark.django_db
def test_stale_checkpoint_recovery_is_registered_in_beat():
    call_command('setup_periodic_tasks', stdout=StringIO())

    periodic = PeriodicTask.objects.get(
        name='resume_stale_media_provider_checkpoints',
    )
    assert periodic.task == (
        'apps.media_processing.tasks.resume_stale_media_provider_checkpoints'
    )
    assert periodic.queue == 'media_processing'
    assert periodic.enabled is True


@pytest.mark.django_db
def test_product_hard_delete_queryset_cannot_cascade_unresolved_media_job(
    product_image,
):
    job = MediaProcessingJob.objects.create(
        tenant=product_image.product.tenant,
        product_image=product_image,
        operations=['resize'],
        status=MediaProcessingJob.Status.FAILED,
        error_code='outcome_uncertain',
        provider_metadata={
            'credit_reservation': {
                'status': 'reserved',
                'key': 'media-delete-guard:reserved',
                'amount': '2',
            },
        },
    )

    with pytest.raises(ProtectedError, match='credit reservation is unresolved'):
        with transaction.atomic():
            Product.all_objects.filter(pk=product_image.product_id).delete()

    assert Product.all_objects.filter(pk=product_image.product_id).exists()
    assert ProductImage.objects.filter(pk=product_image.pk).exists()
    assert MediaProcessingJob.objects.filter(pk=job.pk).exists()


@pytest.mark.django_db
def test_product_image_queryset_delete_cannot_remove_active_media_job(
    product_image,
):
    job = MediaProcessingJob.objects.create(
        tenant=product_image.product.tenant,
        product_image=product_image,
        operations=['resize'],
        status=MediaProcessingJob.Status.PROCESSING,
    )

    with pytest.raises(ProtectedError, match='media provider operation'):
        with transaction.atomic():
            ProductImage.objects.filter(pk=product_image.pk).delete()

    assert ProductImage.objects.filter(pk=product_image.pk).exists()
    assert MediaProcessingJob.objects.filter(pk=job.pk).exists()


@pytest.mark.django_db
def test_product_hard_delete_cannot_remove_recorded_provider_checkpoint(
    product_image,
):
    job = MediaProcessingJob.objects.create(
        tenant=product_image.product.tenant,
        product_image=product_image,
        operations=['resize'],
        status=MediaProcessingJob.Status.FAILED,
        error_code='local_apply_failed',
    )
    _checkpoint_provider_result(job, MediaProviderResult(
        status=MediaProviderResultStatus.PENDING,
        provider_job_id='durable-known-job',
    ))

    with pytest.raises(ProtectedError, match='media provider operation'):
        with transaction.atomic():
            Product.all_objects.filter(pk=product_image.product_id).delete()

    assert Product.all_objects.filter(pk=product_image.product_id).exists()
    assert MediaProcessingJob.objects.filter(pk=job.pk).exists()


@pytest.mark.django_db
def test_product_image_hard_delete_allows_resolved_terminal_media_job(
    product_image,
):
    job = MediaProcessingJob.objects.create(
        tenant=product_image.product.tenant,
        product_image=product_image,
        operations=['resize'],
        status=MediaProcessingJob.Status.FAILED,
        error_code='provider_rejected',
        provider_metadata={'credit_reservation': {'status': 'released'}},
    )

    ProductImage.objects.filter(pk=product_image.pk).delete()

    assert not ProductImage.objects.filter(pk=product_image.pk).exists()
    assert not MediaProcessingJob.objects.filter(pk=job.pk).exists()
