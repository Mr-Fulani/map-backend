"""Сервис ручной модерации изображений товаров.

Approve, reject, set_primary, upload — операции оператора.
"""

import hashlib
import io

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils.timezone import now

from apps.core.image_security import validate_image_pixel_budget
from apps.core.storage import delete_storage_keys
from apps.products.models import ProductImage
from apps.products.storage import (
    MAX_DIMENSION,
    MAX_PHOTOS,
    THUMB_DIMENSION,
    _product_media_keys,
    _resize,
    _save_product_image_pair,
    _to_jpeg_bytes,
    perceptual_hash,
)


def approve(image: ProductImage, reviewed_by=None) -> ProductImage:
    """Одобряет изображение оператором — переводит в AUTO_APPROVED.

    Args:
        image: Экземпляр ProductImage.
        reviewed_by: Пользователь, одобривший изображение (опционально).

    Returns:
        Обновлённый ProductImage.
    """
    image.status = ProductImage.Status.AUTO_APPROVED
    image.reviewed_at = now()
    image.reviewed_by = reviewed_by
    update_fields = ['status', 'reviewed_at', 'reviewed_by']
    if not image.product.images.filter(is_primary=True).exists():
        image.is_primary = True
        update_fields.append('is_primary')
    image.save(update_fields=update_fields)
    return image


def reject(image: ProductImage, reviewed_by=None) -> ProductImage:
    """Отклоняет изображение оператором — переводит в REJECTED.

    Args:
        image: Экземпляр ProductImage.
        reviewed_by: Пользователь, отклонивший изображение (опционально).

    Returns:
        Обновлённый ProductImage.
    """
    was_primary = image.is_primary
    image.status = ProductImage.Status.REJECTED
    if was_primary:
        image.is_primary = False
    image.reviewed_at = now()
    image.reviewed_by = reviewed_by
    update_fields = ['status', 'reviewed_at', 'reviewed_by']
    if was_primary:
        update_fields.append('is_primary')
    image.save(update_fields=update_fields)
    return image


def set_primary(image: ProductImage) -> ProductImage:
    """Устанавливает изображение как главное для товара.

    Снимает is_primary со всех других изображений того же товара.

    Args:
        image: Экземпляр ProductImage который нужно сделать главным.

    Returns:
        Обновлённый ProductImage.
    """
    ProductImage.objects.filter(
        product=image.product, is_primary=True,
    ).exclude(pk=image.pk).update(is_primary=False)

    image.is_primary = True
    image.save(update_fields=['is_primary'])
    return image


def upload_image(product, raw_bytes: bytes) -> ProductImage | None:
    """Загружает изображение вручную (минуя автоматический поиск).

    Использует те же константы и хелперы что и PhotoUploadPipeline:
    SHA256-дедупликация, resize до MAX_DIMENSION + thumb, сохранение в S3.

    Args:
        product: Экземпляр Product.
        raw_bytes: Байты загружаемого изображения.

    Returns:
        Созданный или существующий ProductImage, None при ошибке или превышении лимита.
    """
    from PIL import Image, UnidentifiedImageError

    if (
        not raw_bytes
        or len(raw_bytes) > settings.MAX_IMAGE_UPLOAD_BYTES
        or product.images.exclude(status=ProductImage.Status.REJECTED).count() >= MAX_PHOTOS
    ):
        return None

    sha = hashlib.sha256(raw_bytes).hexdigest()
    if product.images.filter(sha256=sha).exists():
        return product.images.get(sha256=sha)

    try:
        img = Image.open(io.BytesIO(raw_bytes))
        validate_image_pixel_budget(img)
        img.load()
    except (UnidentifiedImageError, Exception):
        return None

    phash = perceptual_hash(img)
    original_bytes = _to_jpeg_bytes(_resize(img.copy(), MAX_DIMENSION))
    thumb_bytes = _to_jpeg_bytes(_resize(img.copy(), THUMB_DIMENSION))

    with transaction.atomic():
        locked_product = type(product).objects.select_for_update().get(pk=product.pk)
        existing = locked_product.images.filter(sha256=sha).first()
        if existing is not None:
            return existing
        if locked_product.images.exclude(
            status=ProductImage.Status.REJECTED,
        ).count() >= MAX_PHOTOS:
            return None

        requested_original_key, requested_thumb_key = _product_media_keys(
            locked_product,
            sha,
        )
        saved_keys = _save_product_image_pair(
            default_storage,
            requested_original_key,
            original_bytes,
            requested_thumb_key,
            thumb_bytes,
        )
        if saved_keys is None:
            return None
        saved_original_key, saved_thumb_key = saved_keys
        position = locked_product.images.count()
        try:
            return ProductImage.objects.create(
                product=locked_product,
                s3_key=saved_original_key,
                s3_key_thumb=saved_thumb_key,
                sha256=sha,
                position=position,
                source_id='manual',
                status=ProductImage.Status.MANUALLY_SET,
                is_primary=(position == 0),
                phash=phash,
            )
        except Exception:
            delete_storage_keys(saved_keys, storage=default_storage)
            raise
