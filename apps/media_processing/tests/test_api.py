import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connections
from django.test import Client

from apps.media_processing.models import (
    MediaProcessingJob,
    MediaProcessingPreset,
    MediaProviderPolicy,
    ProductImageVariant,
    TenantMediaSettings,
)
from apps.core.models import BackgroundJobDispatch
from apps.media_processing.providers.base import (
    BaseMediaProvider,
    MediaOperation,
    MediaProviderRequest,
    MediaProviderResult,
)
from apps.media_processing.providers.registry import (
    clear_media_provider_registry,
    register_media_provider,
)
from apps.products.models import Product, ProductImage
from apps.tenants.services import TenantService
from apps.tenants.models import TenantUser
from apps.tenants.tests.auth import (
    create_operator_key,
    membership_access_token,
    owner_client,
)


class APIProvider(BaseMediaProvider):
    provider_id = 'api-provider'
    supported_operations = frozenset({MediaOperation.RESIZE})

    def process(self, request: MediaProviderRequest) -> MediaProviderResult:
        raise AssertionError('API tests must not execute queued media jobs')


@pytest.fixture(autouse=True)
def configured_api_provider(db):
    clear_media_provider_registry()
    register_media_provider(APIProvider)
    policy = MediaProviderPolicy.objects.create(
        provider_id=APIProvider.provider_id,
        display_name='API provider',
        capabilities=['resize'],
        operation_credit_costs={'resize': 0},
    )
    yield policy
    clear_media_provider_registry()


@pytest.fixture
def media_tenant(db):
    tenant, api_key = TenantService.create_tenant(
        'Media API', 'media-api', 'media-api@test.com', 'pass12345',
    )
    api_key = create_operator_key(tenant)
    return tenant, Client(HTTP_AUTHORIZATION=f'Bearer {api_key}')


@pytest.fixture
def media_image(media_tenant):
    tenant, _ = media_tenant
    product = Product.objects.create(
        tenant=tenant,
        article='API-501',
        brand='BREMBO',
        name='Тормозной диск',
        price='1000.00',
    )
    return ProductImage.objects.create(
        product=product,
        s3_key='products/media-api/source.jpg',
        sha256='media-api-source',
        status=ProductImage.Status.MANUALLY_SET,
    )


@pytest.mark.django_db
def test_process_endpoint_creates_provider_neutral_job(media_tenant, media_image):
    _, client = media_tenant

    with patch('apps.media_processing.views.transaction.on_commit'):
        response = client.post(
            f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
            data=json.dumps({
                'operations': ['resize'],
                'idempotency_key': '30000000-0000-4000-8000-000000000001',
            }),
            content_type='application/json',
        )

    assert response.status_code == 202
    job = MediaProcessingJob.objects.get(pk=response.json()['data']['id'])
    assert job.provider_id == ''
    assert job.operations == ['resize']
    dispatch = BackgroundJobDispatch.objects.get()
    assert dispatch.args == [job.pk]
    assert dispatch.task_name == 'apps.media_processing.tasks.process_media_job'


@pytest.mark.django_db
def test_process_endpoint_enqueues_an_idempotent_job_only_once(media_tenant, media_image):
    _, client = media_tenant
    payload = json.dumps({
        'operations': ['resize'],
        'idempotency_key': '30000000-0000-4000-8000-000000000002',
    })

    with patch('apps.media_processing.views.transaction.on_commit') as on_commit:
        first = client.post(
            f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
            data=payload,
            content_type='application/json',
        )
        second = client.post(
            f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
            data=payload,
            content_type='application/json',
        )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()['data']['id'] == second.json()['data']['id']
    assert MediaProcessingJob.objects.count() == 1
    assert BackgroundJobDispatch.objects.count() == 1
    on_commit.assert_called_once()


@pytest.mark.django_db(transaction=True)
def test_concurrent_media_retries_create_one_canonical_job(media_tenant, media_image):
    _, authenticated_client = media_tenant
    authorization = authenticated_client.defaults['HTTP_AUTHORIZATION']
    barrier = threading.Barrier(2)
    payload = json.dumps({
        'operations': ['resize'],
        'idempotency_key': '30000000-0000-4000-8000-000000000012',
    })

    def submit():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            response = Client(HTTP_AUTHORIZATION=authorization).post(
                f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
                data=payload,
                content_type='application/json',
            )
            return response.status_code, response.json()
        finally:
            connections.close_all()

    with patch(
        'apps.core.tasks.execute_background_dispatch.apply_async',
    ), ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))

    assert [status_code for status_code, _ in results] == [202, 202]
    assert results[0][1]['data']['id'] == results[1][1]['data']['id']
    assert MediaProcessingJob.objects.count() == 1
    assert BackgroundJobDispatch.objects.count() == 1


@pytest.mark.django_db
def test_process_endpoint_requires_uuid_idempotency_key(media_tenant, media_image):
    _, client = media_tenant

    missing = client.post(
        f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
        data=json.dumps({'operations': ['resize']}),
        content_type='application/json',
    )
    malformed = client.post(
        f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
        data=json.dumps({
            'operations': ['resize'],
            'idempotency_key': 'not-a-uuid',
        }),
        content_type='application/json',
    )

    assert missing.status_code == 400
    assert malformed.status_code == 400
    assert MediaProcessingJob.objects.count() == 0


@pytest.mark.django_db
def test_process_endpoint_rejects_key_reuse_for_different_intent(
    media_tenant,
    media_image,
):
    _, client = media_tenant
    key = '30000000-0000-4000-8000-000000000008'

    first = client.post(
        f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
        data=json.dumps({
            'operations': ['resize'],
            'parameters': {'width': 1200},
            'idempotency_key': key,
        }),
        content_type='application/json',
    )
    conflict = client.post(
        f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
        data=json.dumps({
            'operations': ['resize'],
            'parameters': {'width': 800},
            'idempotency_key': key,
        }),
        content_type='application/json',
    )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()['code'] == 'idempotency_conflict'
    assert MediaProcessingJob.objects.count() == 1
    assert BackgroundJobDispatch.objects.count() == 1


@pytest.mark.django_db
def test_media_retry_bypasses_mutable_provider_preflight(media_tenant, media_image):
    _, client = media_tenant
    payload = json.dumps({
        'operations': ['resize'],
        'idempotency_key': '30000000-0000-4000-8000-000000000009',
    })
    first = client.post(
        f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
        data=payload,
        content_type='application/json',
    )

    with patch(
        'apps.media_processing.services._preflight_provider_request',
        side_effect=AssertionError('retry repeated mutable preflight'),
    ):
        retry = client.post(
            f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
            data=payload,
            content_type='application/json',
        )

    assert first.status_code == 202
    assert retry.status_code == 202
    assert retry.json()['data']['id'] == first.json()['data']['id']


@pytest.mark.django_db
def test_media_retry_survives_preset_deactivation(media_tenant, media_image):
    tenant, client = media_tenant
    preset = MediaProcessingPreset.objects.create(
        tenant=tenant,
        name='Replay preset',
        slug='replay-preset',
        operations=['resize'],
        is_active=True,
    )
    payload = json.dumps({
        'preset_id': preset.pk,
        'idempotency_key': '30000000-0000-4000-8000-000000000011',
    })

    first = client.post(
        f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
        data=payload,
        content_type='application/json',
    )
    preset.is_active = False
    preset.save(update_fields=['is_active', 'updated_at'])
    retry = client.post(
        f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
        data=payload,
        content_type='application/json',
    )

    assert first.status_code == 202
    assert retry.status_code == 202
    assert retry.json()['data']['id'] == first.json()['data']['id']
    assert MediaProcessingJob.objects.count() == 1


@pytest.mark.django_db
def test_same_idempotency_key_revives_failed_durable_submission(media_tenant, media_image):
    _, client = media_tenant
    payload = json.dumps({
        'operations': ['resize'],
        'idempotency_key': '30000000-0000-4000-8000-000000000003',
    })
    with patch('apps.media_processing.views.transaction.on_commit'):
        first = client.post(
            f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
            data=payload,
            content_type='application/json',
        )

    job = MediaProcessingJob.objects.get(pk=first.json()['data']['id'])
    dispatch = BackgroundJobDispatch.objects.get()
    job.status = MediaProcessingJob.Status.FAILED
    job.error_code = 'submission_failed'
    job.save(update_fields=['status', 'error_code', 'updated_at'])
    dispatch.status = BackgroundJobDispatch.Status.FAILED
    dispatch.run_attempts = dispatch.max_run_attempts
    dispatch.save(update_fields=['status', 'run_attempts', 'updated_at'])

    with patch('apps.media_processing.views.transaction.on_commit') as on_commit:
        second = client.post(
            f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
            data=payload,
            content_type='application/json',
        )

    dispatch.refresh_from_db()
    assert second.status_code == 202
    assert second.json()['data']['id'] == job.pk
    assert BackgroundJobDispatch.objects.count() == 1
    assert dispatch.status == BackgroundJobDispatch.Status.PENDING
    assert dispatch.run_attempts == 0
    on_commit.assert_called_once()


@pytest.mark.django_db
def test_same_idempotency_key_never_revives_uncertain_provider_outcome(
    media_tenant,
    media_image,
):
    _, client = media_tenant
    payload = json.dumps({
        'operations': ['resize'],
        'idempotency_key': '30000000-0000-4000-8000-000000000004',
    })
    with patch('apps.media_processing.views.transaction.on_commit'):
        first = client.post(
            f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
            data=payload,
            content_type='application/json',
        )

    job = MediaProcessingJob.objects.get(pk=first.json()['data']['id'])
    dispatch = BackgroundJobDispatch.objects.get()
    job.status = MediaProcessingJob.Status.FAILED
    job.error_code = 'outcome_uncertain'
    job.save(update_fields=['status', 'error_code', 'updated_at'])
    dispatch.status = BackgroundJobDispatch.Status.FAILED
    dispatch.save(update_fields=['status', 'updated_at'])

    with patch('apps.media_processing.views.transaction.on_commit') as on_commit:
        second = client.post(
            f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
            data=payload,
            content_type='application/json',
        )

    dispatch.refresh_from_db()
    assert second.status_code == 202
    assert second.json()['data']['id'] == job.pk
    assert dispatch.status == BackgroundJobDispatch.Status.FAILED
    on_commit.assert_not_called()


@pytest.mark.django_db
def test_process_endpoint_rejects_duplicate_operations_before_enqueue(
    media_tenant,
    media_image,
):
    _, client = media_tenant

    with patch('apps.media_processing.views.transaction.on_commit') as on_commit:
        response = client.post(
            f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
            data=json.dumps({
                'operations': ['resize', 'resize'],
                'idempotency_key': '30000000-0000-4000-8000-000000000005',
            }),
            content_type='application/json',
        )

    assert response.status_code == 400
    assert MediaProcessingJob.objects.count() == 0
    on_commit.assert_not_called()


@pytest.mark.django_db
def test_process_endpoint_rejects_explicit_disabled_provider_before_enqueue(
    media_tenant,
    media_image,
    configured_api_provider,
):
    _, client = media_tenant
    configured_api_provider.is_active = False
    configured_api_provider.save(update_fields=['is_active', 'updated_at'])

    with patch('apps.media_processing.views.transaction.on_commit') as on_commit:
        response = client.post(
            f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
            data=json.dumps({
                'operations': ['resize'],
                'provider_id': APIProvider.provider_id,
                'idempotency_key': '30000000-0000-4000-8000-000000000006',
            }),
            content_type='application/json',
        )

    assert response.status_code == 400
    assert MediaProcessingJob.objects.count() == 0
    on_commit.assert_not_called()


@pytest.mark.django_db
def test_preset_endpoint_rejects_denied_provider_preference(
    media_tenant,
    configured_api_provider,
):
    _, client = media_tenant
    configured_api_provider.capabilities = []
    configured_api_provider.save(update_fields=['capabilities', 'updated_at'])

    response = client.post(
        '/api/v1/media/presets/',
        data=json.dumps({
            'name': 'Unsafe preference',
            'slug': 'unsafe-preference',
            'operations': ['resize'],
            'provider_preferences': [APIProvider.provider_id],
        }),
        content_type='application/json',
    )

    assert response.status_code == 400
    assert 'provider_preferences' in json.dumps(response.json())
    assert not MediaProcessingPreset.objects.filter(slug='unsafe-preference').exists()


@pytest.mark.django_db
def test_process_endpoint_returns_400_when_generative_operations_are_disabled(
    media_tenant,
    media_image,
):
    _, client = media_tenant

    response = client.post(
        f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
        data=json.dumps({
            'operations': ['replace_background'],
            'idempotency_key': '30000000-0000-4000-8000-000000000007',
        }),
        content_type='application/json',
    )

    assert response.status_code == 400
    assert MediaProcessingJob.objects.count() == 0


@pytest.mark.django_db
def test_process_endpoint_hides_another_tenants_image(media_tenant, db):
    _, client = media_tenant
    other_tenant, _ = TenantService.create_tenant(
        'Other Media', 'other-media', 'other-media@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=other_tenant, article='OTHER', name='Other', price='1.00',
    )
    image = ProductImage.objects.create(
        product=product, s3_key='other.jpg', sha256='other-media-source',
    )

    response = client.post(
        f'/api/v1/products/{product.pk}/images/{image.pk}/process/',
        data=json.dumps({
            'operations': ['resize'],
            'idempotency_key': '30000000-0000-4000-8000-000000000010',
        }),
        content_type='application/json',
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_media_settings_reject_another_tenants_preset(media_tenant, db):
    tenant, _ = media_tenant
    client = owner_client(tenant)
    other_tenant, _ = TenantService.create_tenant(
        'Preset Owner', 'preset-owner', 'preset-owner@test.com', 'pass12345',
    )
    preset = MediaProcessingPreset.objects.create(
        tenant=other_tenant,
        name='Private preset',
        slug='private-preset',
        operations=['resize'],
    )

    response = client.patch(
        '/api/v1/media/settings/',
        data=json.dumps({'default_preset': preset.pk}),
        content_type='application/json',
    )

    assert response.status_code == 400
    assert TenantMediaSettings.objects.get(tenant=media_tenant[0]).default_preset is None


@pytest.mark.django_db
def test_media_settings_require_human_admin(media_tenant):
    tenant, _ = media_tenant
    membership = TenantService.add_user(
        tenant,
        'media-operator@test.com',
        TenantUser.ROLE_OPERATOR,
    )
    operator = Client(HTTP_AUTHORIZATION=(
        f'Bearer {membership_access_token(membership)}'
    ))

    forbidden = operator.patch(
        '/api/v1/media/settings/',
        data=json.dumps({'auto_process_manual_uploads': True}),
        content_type='application/json',
    )
    allowed = owner_client(tenant).patch(
        '/api/v1/media/settings/',
        data=json.dumps({'auto_process_manual_uploads': True}),
        content_type='application/json',
    )

    assert forbidden.status_code == 403
    assert allowed.status_code == 200


@pytest.mark.django_db
def test_activate_variant_hides_another_tenants_variant(media_tenant, db):
    _, client = media_tenant
    other_tenant, _ = TenantService.create_tenant(
        'Variant Owner', 'variant-owner', 'variant-owner@test.com', 'pass12345',
    )
    product = Product.objects.create(
        tenant=other_tenant, article='VARIANT', name='Variant', price='1.00',
    )
    image = ProductImage.objects.create(
        product=product, s3_key='variant-source.jpg', sha256='variant-source',
    )
    variant = ProductImageVariant.objects.create(
        tenant=other_tenant,
        product_image=image,
        s3_key='variant-result.jpg',
        sha256='variant-result',
    )

    response = client.post(f'/api/v1/media/variants/{variant.pk}/activate/')

    assert response.status_code == 404


@pytest.mark.django_db
def test_deleting_variant_removes_derived_file_after_commit(
    media_image,
    django_capture_on_commit_callbacks,
):
    variant = ProductImageVariant.objects.create(
        tenant=media_image.product.tenant,
        product_image=media_image,
        s3_key='products/media-api/derived.jpg',
        sha256='derived-file',
    )

    with patch(
        'apps.core.storage.default_storage.delete',
    ) as storage_delete, django_capture_on_commit_callbacks(execute=True):
        variant.delete()
        storage_delete.assert_not_called()

    storage_delete.assert_called_once_with('products/media-api/derived.jpg')
