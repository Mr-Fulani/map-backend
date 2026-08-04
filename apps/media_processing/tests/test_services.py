import io
from unittest.mock import patch

import pytest
from PIL import Image

from apps.media_processing.models import ProductImageVariant, TenantMediaSettings
from apps.media_processing.providers.base import (
    BaseMediaProvider,
    MediaOperation,
    MediaProviderRequest,
    MediaProviderResult,
    MediaProviderResultStatus,
)
from apps.media_processing.providers.registry import (
    clear_media_provider_registry,
    register_media_provider,
)
from apps.media_processing.services import (
    activate_variant,
    create_processing_job,
    submit_job,
)
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
    })

    def process(self, request: MediaProviderRequest) -> MediaProviderResult:
        return MediaProviderResult(
            status=MediaProviderResultStatus.SUCCEEDED,
            output_bytes=make_png(),
            output_content_type='image/png',
            metadata={'remote': True},
        )


@pytest.fixture(autouse=True)
def isolated_registry():
    clear_media_provider_registry()
    yield
    clear_media_provider_registry()


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
def test_create_job_does_not_bind_to_provider(product_image):
    job = create_processing_job(
        product_image=product_image,
        operations=['resize'],
    )

    assert job.provider_id == ''
    assert job.operations == ['resize']
    assert job.status == 'queued'


@pytest.mark.django_db
def test_generative_operations_require_tenant_opt_in(product_image):
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


@pytest.mark.django_db
def test_external_result_creates_immutable_variant(product_image):
    register_media_provider(FakeExternalProvider)
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
