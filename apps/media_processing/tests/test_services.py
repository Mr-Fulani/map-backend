import io
from decimal import Decimal
from unittest.mock import patch

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
    MediaProviderRateLimitExceeded,
    activate_variant,
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
def test_provider_failure_releases_reserved_policy_credits(product_image, isolated_registry):
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
        pytest.raises(RuntimeError, match='provider unavailable'),
    ):
        submit_job(job)

    wallet = AIWalletService.summary(tenant)
    assert wallet['available'] == available_before
    assert wallet['reserved'] == 0


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
