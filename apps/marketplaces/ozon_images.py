"""Ozon-only image checks performed before a product mutation."""

from pathlib import PurePosixPath
from typing import Any

from apps.media_processing.models import ProductImageVariant
from apps.products.media import PUBLISHABLE_IMAGE_STATUSES
from apps.products.models import Product, ProductImage


OZON_IMAGE_LIMIT = 15
OZON_MIN_IMAGE_SIDE_PX = 200
OZON_IMAGE_REQUIREMENTS_URL = (
    'https://docs.ozon.ru/global/products/requirements/media/image-requirements/'
)
_OZON_IMAGE_CONTENT_TYPES = frozenset({'image/jpeg', 'image/jpg', 'image/png'})


def _issue(
    code: str,
    image: ProductImage,
    image_index: int,
    message: str,
) -> dict[str, Any]:
    return {
        'code': code,
        'field': 'images',
        'label': f'Фото №{image_index}',
        'message': message,
        'image_id': image.pk,
        'image_index': image_index,
        'help_url': OZON_IMAGE_REQUIREMENTS_URL,
    }


def _delivery_facts(
    image: ProductImage,
    active_variant: ProductImageVariant | None,
) -> tuple[str, int | None, int | None]:
    if active_variant is not None:
        content_type = str(active_variant.content_type or '').strip().casefold()
        return content_type, active_variant.width, active_variant.height

    suffix = PurePosixPath(image.s3_key).suffix.casefold()
    content_type = 'image/jpeg' if suffix in {'.jpeg', '.jpg'} else (
        'image/png' if suffix == '.png' else ''
    )
    return content_type, image.resolution_w, image.resolution_h


def ozon_image_preflight(product: Product) -> dict[str, list[dict[str, Any]]]:
    """Validate only the bounded image set that Ozon publication will use.

    The check reads database metadata only. It never downloads a source URL or
    opens object storage while a tenant is viewing the listing drawer.
    """

    visible_images = list(
        product.images
        .exclude(status=ProductImage.Status.REJECTED)
        .order_by('-is_primary', 'position', 'pk')
        [:OZON_IMAGE_LIMIT + 1]
    )
    visible_index = {image.pk: index for index, image in enumerate(visible_images, start=1)}
    publishable_images = [
        image for image in visible_images if image.status in PUBLISHABLE_IMAGE_STATUSES
    ]
    selected_images = publishable_images[:OZON_IMAGE_LIMIT]
    active_variants = {
        variant.product_image_id: variant
        for variant in ProductImageVariant.objects.filter(
            product_image_id__in=[image.pk for image in selected_images],
            is_active=True,
        ).only(
            'product_image_id', 'content_type', 'width', 'height', 's3_key',
        )
    }

    errors: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []
    for image in selected_images:
        image_index = visible_index[image.pk]
        content_type, width, height = _delivery_facts(
            image,
            active_variants.get(image.pk),
        )
        if content_type not in _OZON_IMAGE_CONTENT_TYPES:
            errors.append(_issue(
                'image_format_invalid',
                image,
                image_index,
                'Ozon принимает по API только JPG или PNG. Замените фото или '
                'сохраните обработанную версию в одном из этих форматов.',
            ))
            continue
        if width is None or height is None:
            recommendations.append(_issue(
                'image_dimensions_unknown',
                image,
                image_index,
                'MAP не знает разрешение старого файла. Фото можно отправить, но '
                'лучше открыть его и при необходимости загрузить заново.',
            ))
            continue
        if width < OZON_MIN_IMAGE_SIDE_PX or height < OZON_MIN_IMAGE_SIDE_PX:
            issue = _issue(
                'image_dimensions_invalid',
                image,
                image_index,
                f'Разрешение {width} × {height} px. Для Ozon каждая сторона должна '
                f'быть не меньше {OZON_MIN_IMAGE_SIDE_PX} px. Замените это фото.',
            )
            issue['width_px'] = width
            issue['height_px'] = height
            errors.append(issue)

    if len(publishable_images) > OZON_IMAGE_LIMIT:
        recommendations.append({
            'code': 'image_limit_exceeded',
            'field': 'images',
            'label': 'Лишние фотографии',
            'message': (
                f'Ozon примет первые {OZON_IMAGE_LIMIT} фото. Остальные останутся '
                'в MAP и не попадут в карточку Ozon.'
            ),
            'help_url': OZON_IMAGE_REQUIREMENTS_URL,
        })
    return {'errors': errors, 'recommendations': recommendations}
