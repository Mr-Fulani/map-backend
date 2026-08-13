import io
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache.backends.locmem import LocMemCache
from PIL import Image

from apps.billing.ai_wallet import AIWalletService
from apps.billing.models import Subscription
from apps.media_processing.models import (
    MediaProcessingJob,
    MediaProcessingPreset,
    MediaProviderPolicy,
    ProductImageVariant,
    TenantMediaSettings,
)
from apps.media_processing.providers.base import (
    BaseMediaProvider,
    MediaOperation,
    MediaProviderRequest,
    MediaProviderResult,
    MediaProviderResultStatus,
)
from apps.media_processing.providers.registry import (
    MediaProviderUnavailable,
    clear_media_provider_registry,
    register_media_provider,
)
from apps.media_processing.services import (
    MediaProviderOutcomeUncertain, MediaProviderRateLimitExceeded,
    _checkpoint_provider_result,
    _provider_result_bytes,
    _serialize_provider_result_checkpoint,
    activate_variant,
    apply_provider_result,
    create_processing_job,
    submit_job,
)
from apps.media_processing.tasks import process_media_job
from apps.products.models import Product, ProductImage
from apps.tenants.services import TenantService


def make_png() -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (640, 640), 'white').save(buffer, format='PNG')
    return buffer.getvalue()


def test_remote_provider_output_uses_bounded_public_transport(settings):
    raw = make_png()
    response = MagicMock(
        status_code=200,
        headers={'Content-Type': 'image/png', 'Content-Length': str(len(raw))},
    )
    response.content = raw
    settings.MEDIA_PROVIDER_OUTPUT_MAX_BYTES = 12345

    with patch(
        'apps.media_processing.services.request_public_http_url',
        return_value=response,
    ) as request:
        payload, content_type = _provider_result_bytes(MediaProviderResult(
            status=MediaProviderResultStatus.SUCCEEDED,
            output_url='https://cdn.example.com/result.png',
        ))

    assert payload == raw
    assert content_type == 'image/png'
    assert request.call_args.kwargs['max_response_bytes'] == 12345
    assert request.call_args.kwargs['redirect_policy'] == 'same-origin'


def test_remote_provider_output_requires_https_before_transport():
    with patch(
        'apps.media_processing.services.request_public_http_url',
    ) as request, pytest.raises(ValueError, match='HTTPS'):
        _provider_result_bytes(MediaProviderResult(
            status=MediaProviderResultStatus.SUCCEEDED,
            output_url='http://cdn.example.com/result.png?token=secret',
        ))

    request.assert_not_called()


def test_remote_provider_transport_error_does_not_expose_signed_url():
    signed_url = 'https://cdn.example.com/result.png?token=historic-secret'
    with patch(
        'apps.media_processing.services.request_public_http_url',
        side_effect=RuntimeError(f'download failed: {signed_url}'),
    ), pytest.raises(ValueError) as raised:
        _provider_result_bytes(MediaProviderResult(
            status=MediaProviderResultStatus.SUCCEEDED,
            output_url=signed_url,
        ))

    assert 'historic-secret' not in str(raised.value)


class FakeExternalProvider(BaseMediaProvider):
    provider_id = 'fake-external'
    display_name = 'Fake external'
    supported_operations = frozenset({
        MediaOperation.RESIZE,
        MediaOperation.REMOVE_BACKGROUND,
        MediaOperation.REPLACE_BACKGROUND,
    })

    def process(self, request: MediaProviderRequest) -> MediaProviderResult:
        return MediaProviderResult(
            status=MediaProviderResultStatus.SUCCEEDED,
            output_bytes=make_png(),
            output_content_type='image/png',
            metadata={'remote': True},
        )


class PreferredProvider(FakeExternalProvider):
    provider_id = 'preferred-provider'

    def process(self, request: MediaProviderRequest) -> MediaProviderResult:
        raise AssertionError('A denied preferred provider must never be called')


class FallbackProvider(FakeExternalProvider):
    provider_id = 'fallback-provider'


class FailingProvider(FakeExternalProvider):
    provider_id = 'failing-provider'

    def process(self, request: MediaProviderRequest) -> MediaProviderResult:
        raise RuntimeError('provider unavailable')


@pytest.fixture(autouse=True)
def isolated_registry():
    clear_media_provider_registry()
    yield
    clear_media_provider_registry()


@pytest.fixture
def configured_media_provider(isolated_registry):
    register_media_provider(FakeExternalProvider)
    return MediaProviderPolicy.objects.create(
        provider_id=FakeExternalProvider.provider_id,
        display_name='Fake external',
        capabilities=[operation.value for operation in FakeExternalProvider.supported_operations],
        operation_credit_costs={
            operation.value: 0 for operation in FakeExternalProvider.supported_operations
        },
    )


@pytest.fixture
def product_image(db):
    tenant, _ = TenantService.create_tenant(
        'Media Tenant', 'media-tenant', 'media@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='P50136',
        brand='BREMBO',
        name='Колодки тормозные',
        price='1000.00',
    )
    return ProductImage.objects.create(
        product=product,
        s3_key='products/media/source.jpg',
        sha256='source-sha',
        status=ProductImage.Status.MANUALLY_SET,
    )


@pytest.mark.django_db
def test_create_job_does_not_bind_to_provider(product_image, configured_media_provider):
    job = create_processing_job(
        product_image=product_image,
        operations=['resize'],
    )

    assert job.provider_id == ''
    assert job.operations == ['resize']
    assert job.status == 'queued'


@pytest.mark.django_db
def test_generative_operations_require_tenant_opt_in(
    product_image,
    configured_media_provider,
):
    with pytest.raises(ValueError, match='отключены'):
        create_processing_job(
            product_image=product_image,
            operations=['replace_background'],
        )

    TenantMediaSettings.objects.create(
        tenant=product_image.product.tenant,
        allow_generative_operations=True,
    )
    job = create_processing_job(
        product_image=product_image,
        operations=['replace_background'],
    )
    assert job.operations == ['replace_background']
    assert job.parameters['prompt_version'] == 'product-media-v1'
    assert 'Сохрани товар без изменений' in job.parameters['generation_prompt']


@pytest.mark.django_db
def test_generative_prompt_cannot_be_overridden_by_client(
    product_image,
    configured_media_provider,
):
    TenantMediaSettings.objects.create(
        tenant=product_image.product.tenant,
        allow_generative_operations=True,
    )

    job = create_processing_job(
        product_image=product_image,
        operations=['replace_background'],
        parameters={'generation_prompt': 'Добавь человека и новый логотип'},
    )

    assert 'Добавь человека' not in job.parameters['generation_prompt']
    assert 'не изменять товар' in job.parameters['negative_prompt']


@pytest.mark.django_db
def test_service_rejects_duplicate_effective_preset_operations(
    product_image,
    configured_media_provider,
):
    preset = MediaProcessingPreset.objects.create(
        tenant=product_image.product.tenant,
        name='Duplicate operations',
        slug='duplicate-operations',
        operations=['resize', 'resize'],
    )

    with pytest.raises(ValueError, match='не должны повторяться'):
        create_processing_job(product_image=product_image, preset=preset)


@pytest.mark.django_db
def test_external_result_creates_immutable_variant(
    product_image,
    configured_media_provider,
):
    job = create_processing_job(
        product_image=product_image,
        operations=['resize', 'remove_background'],
    )

    with (
        patch('apps.media_processing.services.default_storage.url', return_value='https://s3/source.jpg'),
        patch('apps.media_processing.services.default_storage.save', return_value='products/media/result.png'),
    ):
        submit_job(job)

    job.refresh_from_db()
    variant = ProductImageVariant.objects.get(job=job)
    assert job.status == 'succeeded'
    assert job.provider_id == 'fake-external'
    assert variant.s3_key == 'products/media/result.png'
    assert variant.width == 640
    assert product_image.s3_key == 'products/media/source.jpg'


@pytest.mark.django_db
def test_variant_create_failure_removes_saved_object(
    product_image,
    configured_media_provider,
):
    job = create_processing_job(product_image=product_image, operations=['resize'])

    with (
        patch('apps.media_processing.services.default_storage.url', return_value='https://s3/source.jpg'),
        patch(
            'apps.media_processing.services.default_storage.save',
            return_value='products/media/unreferenced.png',
        ),
        patch('apps.media_processing.services.default_storage.delete') as storage_delete,
        patch.object(
            ProductImageVariant.objects,
            'create',
            side_effect=RuntimeError('db failed'),
        ),
        pytest.raises(MediaProviderOutcomeUncertain, match='сверка'),
    ):
        submit_job(job)

    job.refresh_from_db()
    assert (
        job.provider_response_state
        == MediaProcessingJob.ProviderResponseState.RECORDED
    )
    assert job.provider_response_enc is not None
    storage_delete.assert_called_once_with('products/media/unreferenced.png')
    assert not ProductImageVariant.objects.filter(job=job).exists()


@pytest.mark.django_db
def test_same_variant_sha_is_not_reuploaded(
    product_image,
    configured_media_provider,
):
    first = create_processing_job(product_image=product_image, operations=['resize'])
    second = create_processing_job(product_image=product_image, operations=['resize'])

    with (
        patch('apps.media_processing.services.default_storage.url', return_value='https://s3/source.jpg'),
        patch(
            'apps.media_processing.services.default_storage.save',
            return_value='products/media/once.png',
        ) as storage_save,
    ):
        submit_job(first)
        submit_job(second)

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.provider_metadata['variant_id'] == second.provider_metadata['variant_id']
    storage_save.assert_called_once()


@pytest.mark.django_db
@pytest.mark.parametrize('preference_source', ['preset', 'tenant'])
def test_denied_preference_cannot_bypass_policy_and_falls_back(
    product_image,
    isolated_registry,
    preference_source,
):
    register_media_provider(PreferredProvider)
    register_media_provider(FallbackProvider)
    MediaProviderPolicy.objects.create(
        provider_id=PreferredProvider.provider_id,
        display_name='Disabled preferred',
        is_active=False,
        capabilities=['resize'],
        operation_credit_costs={'resize': 0},
    )
    MediaProviderPolicy.objects.create(
        provider_id=FallbackProvider.provider_id,
        display_name='Allowed fallback',
        capabilities=['resize'],
        operation_credit_costs={'resize': 0},
    )
    preset = None
    if preference_source == 'preset':
        preset = MediaProcessingPreset.objects.create(
            tenant=product_image.product.tenant,
            name='Unsafe preference',
            slug='unsafe-preference',
            operations=['resize'],
            provider_preferences=[PreferredProvider.provider_id],
        )
    else:
        TenantMediaSettings.objects.create(
            tenant=product_image.product.tenant,
            provider_preferences={'resize': [PreferredProvider.provider_id]},
        )

    job = create_processing_job(
        product_image=product_image,
        operations=None if preset else ['resize'],
        preset=preset,
    )
    with (
        patch(
            'apps.media_processing.services.default_storage.url',
            return_value='https://s3/source.jpg',
        ),
        patch(
            'apps.media_processing.services.default_storage.save',
            return_value='products/media/fallback.png',
        ),
    ):
        submit_job(job)

    assert job.provider_id == FallbackProvider.provider_id


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('policy_changes', 'error_match'),
    [
        ({'is_active': False}, 'disabled'),
        ({'allowed_plan_slugs': ['enterprise-only']}, 'tenant plan'),
        ({'capabilities': []}, 'requested operations'),
        ({'operation_credit_costs': {}}, 'no credit cost'),
    ],
)
def test_explicit_provider_is_denied_by_each_policy_gate(
    product_image,
    configured_media_provider,
    policy_changes,
    error_match,
):
    for field, value in policy_changes.items():
        setattr(configured_media_provider, field, value)
    configured_media_provider.save(update_fields=[*policy_changes, 'updated_at'])

    with pytest.raises(ValueError, match=error_match):
        create_processing_job(
            product_image=product_image,
            operations=['resize'],
            provider_id=FakeExternalProvider.provider_id,
        )

    assert not MediaProcessingJob.objects.exists()


@pytest.mark.django_db
def test_provider_requires_an_active_subscription_at_execution_time(
    product_image,
    configured_media_provider,
):
    job = create_processing_job(
        product_image=product_image,
        operations=['resize'],
    )
    subscription = product_image.product.tenant.subscription
    subscription.status = Subscription.STATUS_CANCELLED
    subscription.save(update_fields=['status', 'updated_at'])

    with pytest.raises(MediaProviderUnavailable, match='Нет доступного провайдера'):
        submit_job(job)


@pytest.mark.django_db
def test_provider_rate_limit_is_enforced_before_vendor_call(
    product_image,
    configured_media_provider,
):
    configured_media_provider.requests_per_minute = 1
    configured_media_provider.save(update_fields=['requests_per_minute', 'updated_at'])
    first = create_processing_job(product_image=product_image, operations=['resize'])
    second = create_processing_job(product_image=product_image, operations=['resize'])
    local_cache = LocMemCache('media-provider-rate-test', {})

    with (
        patch('apps.media_processing.services.caches', {'coordination': local_cache}),
        patch(
            'apps.media_processing.services.default_storage.url',
            return_value='https://s3/source.jpg',
        ),
        patch(
            'apps.media_processing.services.default_storage.save',
            return_value='products/media/rate.png',
        ),
        patch.object(FakeExternalProvider, 'process', wraps=FakeExternalProvider().process) as process,
    ):
        submit_job(first)
        with pytest.raises(MediaProviderRateLimitExceeded):
            submit_job(second)

    assert process.call_count == 1


@pytest.mark.django_db
def test_policy_credit_cost_is_reserved_and_charged_atomically(
    product_image,
    configured_media_provider,
):
    configured_media_provider.operation_credit_costs = {
        **configured_media_provider.operation_credit_costs,
        'resize': '3.5',
    }
    configured_media_provider.save(update_fields=['operation_credit_costs', 'updated_at'])
    tenant = product_image.product.tenant
    available_before = AIWalletService.summary(tenant)['available']
    job = create_processing_job(product_image=product_image, operations=['resize'])

    with (
        patch(
            'apps.media_processing.services.default_storage.url',
            return_value='https://s3/source.jpg',
        ),
        patch(
            'apps.media_processing.services.default_storage.save',
            return_value='products/media/charged.png',
        ),
    ):
        submit_job(job)

    assert job.estimated_credits == Decimal('3.5')
    assert job.charged_credits == Decimal('3.5')
    assert AIWalletService.summary(tenant)['available'] == available_before - Decimal('3.5')


@pytest.mark.django_db
def test_provider_transport_failure_keeps_reserved_credits_for_reconciliation(
    product_image,
    isolated_registry,
):
    register_media_provider(FailingProvider)
    MediaProviderPolicy.objects.create(
        provider_id=FailingProvider.provider_id,
        display_name='Failing provider',
        capabilities=['resize'],
        operation_credit_costs={'resize': '2'},
    )
    tenant = product_image.product.tenant
    available_before = AIWalletService.summary(tenant)['available']
    job = create_processing_job(product_image=product_image, operations=['resize'])

    with (
        patch(
            'apps.media_processing.services.default_storage.url',
            return_value='https://s3/source.jpg',
        ),
        pytest.raises(MediaProviderOutcomeUncertain, match='неизвестен'),
    ):
        submit_job(job)

    wallet = AIWalletService.summary(tenant)
    assert wallet['available'] == available_before - Decimal('2')
    assert wallet['reserved'] == Decimal('2')


@pytest.mark.django_db
def test_post_provider_persistence_failure_is_uncertain_and_never_releases(
    product_image,
    configured_media_provider,
):
    configured_media_provider.operation_credit_costs = {'resize': '2'}
    configured_media_provider.save(update_fields=[
        'operation_credit_costs', 'updated_at',
    ])
    tenant = product_image.product.tenant
    available_before = AIWalletService.summary(tenant)['available']
    job = create_processing_job(product_image=product_image, operations=['resize'])

    with (
        patch(
            'apps.media_processing.services.default_storage.url',
            return_value='https://s3/source.jpg',
        ),
        patch(
            'apps.media_processing.services.apply_provider_result',
            side_effect=RuntimeError('database write failed'),
        ),
        pytest.raises(MediaProviderOutcomeUncertain, match='сверка'),
    ):
        submit_job(job)

    wallet = AIWalletService.summary(tenant)
    assert wallet['available'] == available_before - Decimal('2')
    assert wallet['reserved'] == Decimal('2')


@pytest.mark.django_db
def test_checkpoint_survives_kill_point_and_resume_never_resubmits_provider(
    product_image,
    configured_media_provider,
):
    configured_media_provider.operation_credit_costs = {'resize': '2'}
    configured_media_provider.save(update_fields=[
        'operation_credit_costs', 'updated_at',
    ])
    tenant = product_image.product.tenant
    available_before = AIWalletService.summary(tenant)['available']
    job = create_processing_job(product_image=product_image, operations=['resize'])

    with (
        patch(
            'apps.media_processing.services.default_storage.url',
            return_value='https://s3/source.jpg',
        ),
        patch(
            'apps.media_processing.services.apply_checkpointed_provider_result',
            side_effect=RuntimeError('worker killed after checkpoint'),
        ),
        pytest.raises(MediaProviderOutcomeUncertain, match='сверка'),
    ):
        submit_job(job)

    job.refresh_from_db()
    assert (
        job.provider_response_state
        == MediaProcessingJob.ProviderResponseState.RECORDED
    )
    assert job.provider_response_enc is not None
    assert AIWalletService.summary(tenant)['reserved'] == Decimal('2')

    with (
        patch.object(
            FakeExternalProvider,
            'process',
            side_effect=AssertionError('provider must not be called on resume'),
        ) as provider_call,
        patch(
            'apps.media_processing.services.default_storage.save',
            return_value='products/media/resumed.png',
        ),
    ):
        submit_job(job)

    job.refresh_from_db()
    assert job.status == MediaProcessingJob.Status.SUCCEEDED
    assert (
        job.provider_response_state
        == MediaProcessingJob.ProviderResponseState.APPLIED
    )
    assert job.provider_response_enc is None
    assert job.provider_response_resolved_at is not None
    assert AIWalletService.summary(tenant)['reserved'] == Decimal('0')
    assert AIWalletService.summary(tenant)['available'] == available_before - Decimal('2')
    provider_call.assert_not_called()


def test_provider_response_checkpoint_is_bounded_when_binary_is_oversized(settings):
    settings.MEDIA_PROVIDER_OUTPUT_MAX_BYTES = 25 * 1024 * 1024
    oversized = b'x' * (2 * 1024 * 1024 + 1)

    payload, canonical, digest = _serialize_provider_result_checkpoint(
        MediaProviderResult(
            status=MediaProviderResultStatus.SUCCEEDED,
            output_bytes=oversized,
            output_content_type='image/png',
        ),
    )

    assert payload['output_bytes_omitted'] is True
    assert payload['output_bytes_b64'] == ''
    assert payload['output_bytes_size'] == len(oversized)
    assert len(canonical) < 128 * 1024
    assert len(digest) == 64


@pytest.mark.django_db
def test_signed_provider_url_is_encrypted_in_durable_checkpoint(
    product_image,
    configured_media_provider,
):
    job = create_processing_job(product_image=product_image, operations=['resize'])
    signed_url = 'https://cdn.example.com/result.png?token=checkpoint-secret'

    _checkpoint_provider_result(job, MediaProviderResult(
        status=MediaProviderResultStatus.SUCCEEDED,
        output_url=signed_url,
    ))

    job.refresh_from_db()
    assert job.provider_response_enc is not None
    assert b'checkpoint-secret' not in bytes(job.provider_response_enc)


@pytest.mark.django_db
@pytest.mark.parametrize('failure_point', ['download', 's3'])
def test_successful_provider_local_failure_keeps_reservation_for_reconciliation(
    product_image,
    configured_media_provider,
    failure_point,
):
    configured_media_provider.operation_credit_costs = {'resize': '2'}
    configured_media_provider.save(update_fields=[
        'operation_credit_costs', 'updated_at',
    ])
    tenant = product_image.product.tenant
    available_before = AIWalletService.summary(tenant)['available']
    job = create_processing_job(product_image=product_image, operations=['resize'])
    patches = [patch(
        'apps.media_processing.services.default_storage.url',
        return_value='https://s3/source.jpg',
    )]
    if failure_point == 'download':
        patches.append(patch(
            'apps.media_processing.services._provider_result_bytes',
            side_effect=RuntimeError('output download failed'),
        ))
    else:
        patches.append(patch(
            'apps.media_processing.services._store_variant',
            side_effect=RuntimeError('S3 save failed'),
        ))

    with patches[0], patches[1], pytest.raises(
        MediaProviderOutcomeUncertain,
        match='сверка',
    ):
        submit_job(job)

    job.refresh_from_db()
    wallet = AIWalletService.summary(tenant)
    assert job.error_code != 'invalid_provider_output'
    assert wallet['available'] == available_before - Decimal('2')
    assert wallet['reserved'] == Decimal('2')


@pytest.mark.django_db
def test_task_never_resubmits_after_post_provider_persistence_failure(
    product_image,
    configured_media_provider,
):
    job = create_processing_job(product_image=product_image, operations=['resize'])
    provider_result = MediaProviderResult(
        status=MediaProviderResultStatus.PENDING,
        provider_job_id='remote-job-accepted',
    )

    with (
        patch(
            'apps.media_processing.services.default_storage.url',
            return_value='https://s3/source.jpg',
        ),
        patch.object(
            FakeExternalProvider,
            'process',
            return_value=provider_result,
        ) as provider_call,
        patch(
            'apps.media_processing.services.apply_provider_result',
            side_effect=RuntimeError('database write failed'),
        ),
        pytest.raises(RuntimeError, match='требуется сверка'),
    ):
        process_media_job.run(job.pk)

    job.refresh_from_db()
    assert job.status == MediaProcessingJob.Status.FAILED
    assert job.error_code == 'outcome_uncertain'
    assert (
        process_media_job.run(job.pk)['status']
        == MediaProcessingJob.Status.SUBMITTED
    )
    provider_call.assert_called_once()


@pytest.mark.django_db
def test_provider_failure_message_is_not_persisted_verbatim(
    product_image,
    configured_media_provider,
):
    job = create_processing_job(product_image=product_image, operations=['resize'])

    apply_provider_result(job, MediaProviderResult(
        status=MediaProviderResultStatus.FAILED,
        error_code='vendor_failed',
        error_message='token=provider-secret https://vendor.test/result?key=secret',
    ))

    job.refresh_from_db()
    assert job.status == MediaProcessingJob.Status.FAILED
    assert job.error_message == 'Медиа-провайдер не смог обработать изображение.'
    assert 'provider-secret' not in job.error_message


@pytest.mark.django_db
def test_media_task_claim_prevents_duplicate_vendor_submission(
    product_image,
    configured_media_provider,
):
    job = create_processing_job(product_image=product_image, operations=['resize'])

    with patch('apps.media_processing.services.submit_job', return_value=job) as submit:
        process_media_job.run(job.pk)
        process_media_job.run(job.pk)

    submit.assert_called_once()


@pytest.mark.django_db
def test_fresh_duplicate_task_does_not_reexecute_terminal_provider_failure(
    product_image,
    configured_media_provider,
):
    job = create_processing_job(product_image=product_image, operations=['resize'])
    job.status = MediaProcessingJob.Status.FAILED
    job.error_code = 'provider_error'
    job.save(update_fields=['status', 'error_code', 'updated_at'])

    with patch('apps.media_processing.services.submit_job') as submit:
        result = process_media_job.run(job.pk)

    assert result['status'] == MediaProcessingJob.Status.FAILED
    submit.assert_not_called()


@pytest.mark.django_db
def test_celery_retry_can_reclaim_retryable_submission_failure(
    product_image,
    configured_media_provider,
):
    job = create_processing_job(product_image=product_image, operations=['resize'])
    job.status = MediaProcessingJob.Status.FAILED
    job.error_code = 'submission_failed'
    job.save(update_fields=['status', 'error_code', 'updated_at'])

    process_media_job.push_request(retries=1)
    try:
        with patch(
            'apps.media_processing.services.submit_job', return_value=job,
        ) as submit:
            process_media_job.run(job.pk)
    finally:
        process_media_job.pop_request()

    submit.assert_called_once()


@pytest.mark.django_db
def test_activate_variant_keeps_only_one_active(product_image):
    first = ProductImageVariant.objects.create(
        tenant=product_image.product.tenant,
        product_image=product_image,
        s3_key='products/media/one.png',
        sha256='one',
        is_active=True,
    )
    second = ProductImageVariant.objects.create(
        tenant=product_image.product.tenant,
        product_image=product_image,
        s3_key='products/media/two.png',
        sha256='two',
    )

    activate_variant(second)

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.is_active is False
    assert second.is_active is True
