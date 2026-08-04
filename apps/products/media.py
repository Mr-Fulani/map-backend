from apps.products.models import ProductImage


PUBLISHABLE_IMAGE_STATUSES = (
    ProductImage.Status.AUTO_APPROVED,
    ProductImage.Status.MANUALLY_SET,
    ProductImage.Status.IMPORTED,
)


def get_publishable_product_images(product):
    """Возвращает фото, которые можно использовать в листингах/фидах."""
    return (
        product.images
        .filter(status__in=PUBLISHABLE_IMAGE_STATUSES)
        .order_by('-is_primary', 'position', 'pk')
    )


def get_product_image_delivery_key(product_image) -> str:
    """Resolve an active processed variant without overwriting the source image."""
    from apps.media_processing.services import delivery_s3_key
    return delivery_s3_key(product_image)
