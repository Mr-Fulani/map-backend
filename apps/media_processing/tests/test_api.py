import json
from unittest.mock import patch

import pytest
from django.test import Client

from apps.media_processing.models import (
    MediaProcessingJob,
    MediaProcessingPreset,
    ProductImageVariant,
    TenantMediaSettings,
)
from apps.products.models import Product, ProductImage
from apps.tenants.services import TenantService


@pytest.fixture
def media_tenant(db):
    tenant, api_key = TenantService.create_tenant(
        'Media API', 'media-api', 'media-api@test.com', 'pass12345',
    )
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
            data=json.dumps({'operations': ['resize']}),
            content_type='application/json',
        )

    assert response.status_code == 202
    job = MediaProcessingJob.objects.get(pk=response.json()['data']['id'])
    assert job.provider_id == ''
    assert job.operations == ['resize']


@pytest.mark.django_db
def test_process_endpoint_returns_400_when_generative_operations_are_disabled(
    media_tenant,
    media_image,
):
    _, client = media_tenant

    response = client.post(
        f'/api/v1/products/{media_image.product_id}/images/{media_image.pk}/process/',
        data=json.dumps({'operations': ['replace_background']}),
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
        data=json.dumps({'operations': ['resize']}),
        content_type='application/json',
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_media_settings_reject_another_tenants_preset(media_tenant, db):
    _, client = media_tenant
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
def test_deleting_variant_removes_derived_file(media_image):
    variant = ProductImageVariant.objects.create(
        tenant=media_image.product.tenant,
        product_image=media_image,
        s3_key='products/media-api/derived.jpg',
        sha256='derived-file',
    )

    with (
        patch('apps.media_processing.signals.default_storage.exists', return_value=True),
        patch('apps.media_processing.signals.default_storage.delete') as storage_delete,
    ):
        variant.delete()

    storage_delete.assert_called_once_with('products/media-api/derived.jpg')
