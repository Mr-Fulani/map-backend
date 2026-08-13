from unittest.mock import patch

import pytest

from apps.core.storage import delete_unreferenced_storage_keys
from apps.media_processing.models import ProductImageVariant
from apps.products.models import Product, ProductImage, TenantCatalogCategory
from apps.tenants.services import TenantService


def _tenant(slug: str):
    tenant, _ = TenantService.create_tenant(
        f'Storage safety {slug}',
        slug,
        f'{slug}@example.com',
        'pass12345',
    )
    return tenant


def _product(tenant, article: str) -> Product:
    return Product.objects.create(
        tenant=tenant,
        article=article,
        name=f'Product {article}',
        price='1.00',
    )


def test_reference_check_failure_keeps_storage_object():
    storage_key = 'dev/products/keep-on-db-error.jpg'
    with (
        patch(
            'apps.core.storage.storage_key_is_referenced',
            side_effect=RuntimeError('database unavailable'),
        ),
        patch('apps.core.storage.default_storage.delete') as storage_delete,
    ):
        delete_unreferenced_storage_keys((storage_key,))

    storage_delete.assert_not_called()


@pytest.mark.django_db
def test_shared_product_image_key_is_kept_until_last_reference_is_deleted(
    django_capture_on_commit_callbacks,
):
    tenant = _tenant('shared-product-image-key')
    product = _product(tenant, 'SHARED-IMAGE')
    shared_key = 'dev/products/legacy/shared-original.jpg'
    first = ProductImage.objects.create(
        product=product,
        s3_key=shared_key,
        sha256='shared-image-first',
    )
    second = ProductImage.objects.create(
        product=product,
        s3_key=shared_key,
        sha256='shared-image-second',
    )

    with patch('apps.core.storage.default_storage.delete') as storage_delete:
        with django_capture_on_commit_callbacks(execute=True):
            first.delete()

        storage_delete.assert_not_called()

        with django_capture_on_commit_callbacks(execute=True):
            second.delete()

    storage_delete.assert_called_once_with(shared_key)


@pytest.mark.django_db
@pytest.mark.parametrize('referencing_field', ['s3_key_preview', 's3_key_thumb'])
def test_original_key_is_kept_when_another_product_image_uses_it_as_a_derivative(
    django_capture_on_commit_callbacks,
    referencing_field,
):
    tenant = _tenant(f'shared-derivative-{referencing_field}')
    product = _product(tenant, f'DERIVATIVE-{referencing_field}')
    shared_key = f'dev/products/legacy/{referencing_field}.jpg'
    deleted = ProductImage.objects.create(
        product=product,
        s3_key=shared_key,
        sha256=f'deleted-{referencing_field}',
    )
    ProductImage.objects.create(
        product=product,
        s3_key=f'dev/products/unique/{referencing_field}.jpg',
        sha256=f'reference-{referencing_field}',
        **{referencing_field: shared_key},
    )

    with (
        patch('apps.core.storage.default_storage.delete') as storage_delete,
        django_capture_on_commit_callbacks(execute=True),
    ):
        deleted.delete()

    storage_delete.assert_not_called()


@pytest.mark.django_db
def test_variant_key_is_kept_when_product_image_still_references_it(
    django_capture_on_commit_callbacks,
):
    tenant = _tenant('variant-shared-with-image')
    product = _product(tenant, 'VARIANT-SHARED')
    shared_key = 'dev/products/legacy/variant-and-original.jpg'
    image = ProductImage.objects.create(
        product=product,
        s3_key=shared_key,
        sha256='variant-shared-image',
    )
    variant = ProductImageVariant.objects.create(
        tenant=tenant,
        product_image=image,
        s3_key=shared_key,
        sha256='variant-shared-variant',
    )

    with (
        patch('apps.core.storage.default_storage.delete') as storage_delete,
        django_capture_on_commit_callbacks(execute=True),
    ):
        variant.delete()

    storage_delete.assert_not_called()


@pytest.mark.django_db
def test_category_key_is_kept_when_variant_still_references_it(
    django_capture_on_commit_callbacks,
):
    tenant = _tenant('category-shared-with-variant')
    product = _product(tenant, 'CATEGORY-SHARED')
    image = ProductImage.objects.create(
        product=product,
        s3_key='dev/products/category-shared/source.jpg',
        sha256='category-shared-source',
    )
    shared_key = 'dev/products/legacy/category-and-variant.jpg'
    ProductImageVariant.objects.create(
        tenant=tenant,
        product_image=image,
        s3_key=shared_key,
        sha256='category-shared-variant',
    )
    category = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Shared media category',
        default_image_s3_key=shared_key,
    )

    with (
        patch('apps.core.storage.default_storage.delete') as storage_delete,
        django_capture_on_commit_callbacks(execute=True),
    ):
        category.delete()

    storage_delete.assert_not_called()
