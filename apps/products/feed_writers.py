"""Account-first fencing for product mutations that change marketplace XML.

Product data is shared by every listing for that product.  A writer therefore
captures a read-only product generation and its current feed-owner accounts,
locks/bump those accounts first, and only then locks the product rows.  If
either snapshot went stale, the whole transaction (including the cursor bump)
rolls back and the caller may retry from a fresh snapshot.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.marketplaces.feed_intents import (
    bump_feed_intents,
    product_feed_account_ids,
)

if TYPE_CHECKING:
    from apps.media_processing.models import ProductImageVariant
    from apps.products.models import (
        Product,
        ProductImage,
        TenantCatalogCategory,
    )


PRODUCT_FEED_GENERATION_FIELDS = (
    'article',
    'name',
    'brand',
    'category_1c',
    'catalog_category_id',
    'condition',
    'description_1c',
    'oem_numbers',
    'stock_qty',
    'price',
    'sync_excluded',
)


class StaleProductFeedWrite(RuntimeError):
    """The read-only product or listing-membership snapshot is no longer current."""


@dataclass(frozen=True)
class ProductFeedGeneration:
    product_id: int
    tenant_id: int
    updated_at: datetime
    deleted_at: datetime | None
    values: tuple[object, ...]


@dataclass(frozen=True)
class ProductImageFeedGeneration:
    image_id: int
    product_id: int
    status: str
    is_primary: bool
    position: int
    s3_key: str
    url_source: str


@dataclass(frozen=True)
class ProductImageVariantFeedGeneration:
    variant_id: int
    product_image_id: int
    is_active: bool
    s3_key: str


@dataclass(frozen=True)
class CatalogCategoryFeedGeneration:
    category_id: int
    tenant_id: int
    updated_at: datetime
    name: str
    parent_id: int | None
    external_id: str
    default_image_s3_key: str


def _generation(product) -> ProductFeedGeneration:
    return ProductFeedGeneration(
        product_id=product.pk,
        tenant_id=product.tenant_id,
        updated_at=product.updated_at,
        deleted_at=product.deleted_at,
        values=tuple(getattr(product, field) for field in PRODUCT_FEED_GENERATION_FIELDS),
    )


def capture_product_feed_generations(
    product_ids: Iterable[int],
) -> dict[int, ProductFeedGeneration]:
    """Read exact generations without acquiring a product row lock."""

    from apps.products.models import Product

    normalized_ids = tuple(sorted({int(product_id) for product_id in product_ids}))
    if not normalized_ids:
        return {}
    products = (
        Product.all_objects.filter(pk__in=normalized_ids)
        .only(
            'pk',
            'tenant_id',
            'updated_at',
            'deleted_at',
            *PRODUCT_FEED_GENERATION_FIELDS,
        )
        .order_by('pk')
    )
    return {product.pk: _generation(product) for product in products}


def capture_product_feed_generation(product) -> ProductFeedGeneration:
    """Capture an already-read instance using the same exact comparison set."""

    return _generation(product)


def _image_generation(image) -> ProductImageFeedGeneration:
    return ProductImageFeedGeneration(
        image_id=image.pk,
        product_id=image.product_id,
        status=image.status,
        is_primary=image.is_primary,
        position=image.position,
        s3_key=image.s3_key,
        url_source=image.url_source,
    )


def _variant_generation(variant) -> ProductImageVariantFeedGeneration:
    return ProductImageVariantFeedGeneration(
        variant_id=variant.pk,
        product_image_id=variant.product_image_id,
        is_active=variant.is_active,
        s3_key=variant.s3_key,
    )


def capture_product_image_feed_generations(
    product_id: int,
) -> tuple[ProductImageFeedGeneration, ...]:
    from apps.products.models import ProductImage

    return tuple(
        _image_generation(image)
        for image in ProductImage.objects.filter(product_id=product_id).order_by('pk')
    )


def capture_product_image_variant_feed_generations(
    product_image_id: int,
) -> tuple[ProductImageVariantFeedGeneration, ...]:
    from apps.media_processing.models import ProductImageVariant

    return tuple(
        _variant_generation(variant)
        for variant in ProductImageVariant.objects.filter(
            product_image_id=product_image_id,
        ).order_by('pk')
    )


def _category_generation(category) -> CatalogCategoryFeedGeneration:
    return CatalogCategoryFeedGeneration(
        category_id=category.pk,
        tenant_id=category.tenant_id,
        updated_at=category.updated_at,
        name=category.name,
        parent_id=category.parent_id,
        external_id=category.external_id,
        default_image_s3_key=category.default_image_s3_key,
    )


def _category_subtree_ids(tenant_id: int, category_id: int) -> tuple[int, ...]:
    from apps.products.models import TenantCatalogCategory

    children_by_parent: dict[int, list[int]] = {}
    for child_id, parent_id in TenantCatalogCategory.objects.filter(
        tenant_id=tenant_id,
    ).values_list('pk', 'parent_id'):
        if parent_id is not None:
            children_by_parent.setdefault(parent_id, []).append(child_id)
    found = {category_id}
    pending = [category_id]
    while pending:
        parent_id = pending.pop()
        for child_id in children_by_parent.get(parent_id, ()):
            if child_id not in found:
                found.add(child_id)
                pending.append(child_id)
    return tuple(sorted(found))


def _ingress_enabled() -> bool:
    return settings.MARKETPLACE_FEED_INGRESS_MODE in {'dual_write', 'durable'}


@contextmanager
def locked_product_feed_write(
    generations: Iterable[ProductFeedGeneration],
    *,
    observed_at: datetime | None = None,
    bump_product_ids: Iterable[int] | None = None,
) -> Iterator[dict[int, Product]]:
    """Lock account -> endpoint -> product and reject stale generations.

    The read-only account snapshot is deliberately taken before entering the
    transaction.  After account/endpoint locks are held it is checked again;
    a listing move in the intervening window rolls back the cursor bump rather
    than losing a destination account update.
    """

    from apps.products.models import Product

    expected = {generation.product_id: generation for generation in generations}
    product_ids = tuple(sorted(expected))
    if bump_product_ids is None:
        bump_ids = product_ids
    else:
        bump_ids = tuple(sorted({int(product_id) for product_id in bump_product_ids}))
    if not set(bump_ids).issubset(expected):
        raise ValueError('bump_product_ids must be present in generations.')

    expected_account_ids = (
        product_feed_account_ids(bump_ids) if _ingress_enabled() else ()
    )
    observed_at = observed_at or timezone.now()
    if timezone.is_naive(observed_at):
        raise ValueError('observed_at must be a timezone-aware datetime.')

    with transaction.atomic():
        if _ingress_enabled():
            # This is the first row lock in the domain transaction.
            bump_feed_intents(expected_account_ids, observed_at)
            if product_feed_account_ids(bump_ids) != expected_account_ids:
                raise StaleProductFeedWrite(
                    'Product feed-owner membership changed before account fencing.',
                )

        locked_products = list(
            Product.all_objects.select_for_update()
            .filter(pk__in=product_ids)
            .only(
                'pk',
                'tenant_id',
                'updated_at',
                'deleted_at',
                *PRODUCT_FEED_GENERATION_FIELDS,
            )
            .order_by('pk')
        )
        locked_by_id = {product.pk: product for product in locked_products}
        if tuple(locked_by_id) != product_ids:
            raise StaleProductFeedWrite('A product disappeared before its feed write.')
        for product_id, generation in expected.items():
            if _generation(locked_by_id[product_id]) != generation:
                raise StaleProductFeedWrite(
                    f'Product {product_id} changed before its feed write.',
                )

        # Recheck after product locks as a regression fence for writers that
        # move listings under the documented account-first order.
        if _ingress_enabled() and product_feed_account_ids(bump_ids) != expected_account_ids:
            raise StaleProductFeedWrite(
                'Product feed-owner membership changed while locking products.',
            )
        yield locked_by_id


@contextmanager
def locked_product_images_feed_write(
    product_id: int,
    *,
    bump: bool,
    observed_at: datetime | None = None,
) -> Iterator[tuple[Product, dict[int, ProductImage]]]:
    """Lock account -> endpoint -> product -> every image for one product."""

    from apps.products.models import ProductImage

    product_generations = capture_product_feed_generations((product_id,))
    product_generation = product_generations.get(product_id)
    if product_generation is None:
        raise StaleProductFeedWrite(f'Product {product_id} no longer exists.')
    if product_generation.deleted_at is not None:
        raise StaleProductFeedWrite(f'Product {product_id} is deleted.')
    expected_images = capture_product_image_feed_generations(product_id)

    with locked_product_feed_write(
        (product_generation,),
        observed_at=observed_at,
        bump_product_ids=(product_id,) if bump else (),
    ) as products:
        images = list(
            ProductImage.objects.select_for_update()
            .filter(product_id=product_id)
            .order_by('pk')
        )
        current_images = tuple(_image_generation(image) for image in images)
        if current_images != expected_images:
            raise StaleProductFeedWrite(
                f'Product {product_id} images changed before their feed write.',
            )
        yield products[product_id], {image.pk: image for image in images}


@contextmanager
def locked_product_image_variants_feed_write(
    product_image_id: int,
    *,
    bump: bool,
    observed_at: datetime | None = None,
) -> Iterator[
    tuple[Product, ProductImage, dict[int, ProductImageVariant]]
]:
    """Lock account -> endpoint -> product -> images -> variants."""

    from apps.products.models import ProductImage
    from apps.media_processing.models import ProductImageVariant

    image = ProductImage.objects.filter(pk=product_image_id).only(
        'pk', 'product_id',
    ).first()
    if image is None:
        raise StaleProductFeedWrite(
            f'Product image {product_image_id} no longer exists.',
        )
    expected_variants = capture_product_image_variant_feed_generations(
        product_image_id,
    )
    with locked_product_images_feed_write(
        image.product_id,
        bump=bump,
        observed_at=observed_at,
    ) as (product, images):
        locked_image = images.get(product_image_id)
        if locked_image is None:
            raise StaleProductFeedWrite(
                f'Product image {product_image_id} no longer exists.',
            )
        variants = list(
            ProductImageVariant.objects.select_for_update()
            .filter(product_image_id=product_image_id)
            .order_by('pk')
        )
        current_variants = tuple(
            _variant_generation(variant) for variant in variants
        )
        if current_variants != expected_variants:
            raise StaleProductFeedWrite(
                f'Product image {product_image_id} variants changed before activation.',
            )
        yield product, locked_image, {
            variant.pk: variant for variant in variants
        }


@contextmanager
def locked_catalog_category_feed_write(
    category_id: int,
    *,
    include_descendants: bool,
    bump: bool,
    observed_at: datetime | None = None,
) -> Iterator[tuple[TenantCatalogCategory, dict[int, Product]]]:
    """Lock account -> endpoint -> products -> category subtree."""

    from apps.products.models import Product, TenantCatalogCategory

    category = TenantCatalogCategory.objects.filter(pk=category_id).first()
    if category is None:
        raise StaleProductFeedWrite(
            f'Catalog category {category_id} no longer exists.',
        )
    expected_category = _category_generation(category)
    category_ids = (
        _category_subtree_ids(category.tenant_id, category_id)
        if include_descendants else (category_id,)
    )
    product_ids = tuple(
        Product.objects.filter(
            tenant_id=category.tenant_id,
            catalog_category_id__in=category_ids,
        ).order_by('pk').values_list('pk', flat=True)
    )
    product_generations = capture_product_feed_generations(product_ids)

    with locked_product_feed_write(
        product_generations.values(),
        observed_at=observed_at,
        bump_product_ids=product_ids if bump else (),
    ) as products:
        categories = list(
            TenantCatalogCategory.objects.select_for_update()
            .filter(pk__in=category_ids, tenant_id=category.tenant_id)
            .order_by('pk')
        )
        categories_by_id = {candidate.pk: candidate for candidate in categories}
        locked_category = categories_by_id.get(category_id)
        if (
            locked_category is None
            or _category_generation(locked_category) != expected_category
        ):
            raise StaleProductFeedWrite(
                f'Catalog category {category_id} changed before its feed write.',
            )
        current_category_ids = (
            _category_subtree_ids(category.tenant_id, category_id)
            if include_descendants else (category_id,)
        )
        current_product_ids = tuple(
            Product.objects.filter(
                tenant_id=category.tenant_id,
                catalog_category_id__in=current_category_ids,
            ).order_by('pk').values_list('pk', flat=True)
        )
        if (
            current_category_ids != category_ids
            or current_product_ids != product_ids
        ):
            raise StaleProductFeedWrite(
                f'Catalog category {category_id} membership changed before its feed write.',
            )
        yield locked_category, products
