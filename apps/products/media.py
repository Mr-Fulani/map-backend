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
