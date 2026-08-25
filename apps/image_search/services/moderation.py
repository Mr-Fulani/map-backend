"""Сервис ручной модерации изображений товаров.

Approve, reject, set_primary, upload — операции оператора.
"""

import hashlib
import io
import uuid

from django.conf import settings
from django.core.files.storage import default_storage
from django.utils.timezone import now

from apps.core.image_security import validate_image_pixel_budget
from apps.core.storage import delete_storage_keys
from apps.products.feed_writers import (
    StaleProductFeedWrite,
    locked_product_images_feed_write,
)
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
    for _attempt in range(3):
        current = ProductImage.objects.filter(pk=image.pk).first()
        if current is None:
            raise StaleProductFeedWrite(f'Product image {image.pk} no longer exists.')
        try:
            with locked_product_images_feed_write(
                current.product_id,
                bump=True,
            ) as (_product, images):
                locked = images.get(current.pk)
                if locked is None:
                    raise StaleProductFeedWrite(
                        f'Product image {current.pk} no longer exists.',
                    )
                locked.status = ProductImage.Status.AUTO_APPROVED
                locked.reviewed_at = now()
                locked.reviewed_by = reviewed_by
                update_fields = ['status', 'reviewed_at', 'reviewed_by']
                if not any(candidate.is_primary for candidate in images.values()):
                    locked.is_primary = True
                    update_fields.append('is_primary')
                locked.save(update_fields=update_fields)
                image = locked
            return image
        except StaleProductFeedWrite:
            continue
    raise StaleProductFeedWrite(
        f'Product image {image.pk} changed repeatedly during approval.',
    )


def reject(image: ProductImage, reviewed_by=None) -> ProductImage:
    """Отклоняет изображение оператором — переводит в REJECTED.

    Args:
        image: Экземпляр ProductImage.
        reviewed_by: Пользователь, отклонивший изображение (опционально).

    Returns:
        Обновлённый ProductImage.
    """
    for _attempt in range(3):
        current = ProductImage.objects.filter(pk=image.pk).first()
        if current is None:
            raise StaleProductFeedWrite(f'Product image {image.pk} no longer exists.')
        try:
            with locked_product_images_feed_write(
                current.product_id,
                bump=True,
            ) as (_product, images):
                locked = images.get(current.pk)
                if locked is None:
                    raise StaleProductFeedWrite(
                        f'Product image {current.pk} no longer exists.',
                    )
                was_primary = locked.is_primary
                locked.status = ProductImage.Status.REJECTED
                if was_primary:
                    locked.is_primary = False
                locked.reviewed_at = now()
                locked.reviewed_by = reviewed_by
                update_fields = ['status', 'reviewed_at', 'reviewed_by']
                if was_primary:
                    update_fields.append('is_primary')
                locked.save(update_fields=update_fields)
                image = locked
            return image
        except StaleProductFeedWrite:
            continue
    raise StaleProductFeedWrite(
        f'Product image {image.pk} changed repeatedly during rejection.',
    )


def set_primary(image: ProductImage) -> ProductImage:
    """Устанавливает изображение как главное для товара.

    Снимает is_primary со всех других изображений того же товара.

    Args:
        image: Экземпляр ProductImage который нужно сделать главным.

    Returns:
        Обновлённый ProductImage.
    """
    for _attempt in range(3):
        current = ProductImage.objects.filter(pk=image.pk).first()
        if current is None:
            raise StaleProductFeedWrite(f'Product image {image.pk} no longer exists.')
        try:
            with locked_product_images_feed_write(
                current.product_id,
                bump=True,
            ) as (_product, images):
                locked = images.get(current.pk)
                if locked is None:
                    raise StaleProductFeedWrite(
                        f'Product image {current.pk} no longer exists.',
                    )
                ProductImage.objects.filter(
                    product_id=current.product_id,
                    is_primary=True,
                ).exclude(pk=current.pk).update(is_primary=False)
                locked.is_primary = True
                locked.save(update_fields=['is_primary'])
                image = locked
            return image
        except StaleProductFeedWrite:
            continue
    raise StaleProductFeedWrite(
        f'Product image {image.pk} changed repeatedly while setting primary.',
    )


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

    if not raw_bytes or len(raw_bytes) > settings.MAX_IMAGE_UPLOAD_BYTES:
        return None

    sha = hashlib.sha256(raw_bytes).hexdigest()
    existing = ProductImage.objects.filter(product_id=product.pk, sha256=sha).first()
    if existing is not None:
        return existing
    if ProductImage.objects.filter(product_id=product.pk).exclude(
        status=ProductImage.Status.REJECTED,
    ).count() >= MAX_PHOTOS:
        return None

    try:
        img = Image.open(io.BytesIO(raw_bytes))
        validate_image_pixel_budget(img)
        img.load()
    except (UnidentifiedImageError, Exception):
        return None

    phash = perceptual_hash(img)
    original_bytes = _to_jpeg_bytes(_resize(img.copy(), MAX_DIMENSION))
    thumb_bytes = _to_jpeg_bytes(_resize(img.copy(), THUMB_DIMENSION))

    class _UploadAborted(RuntimeError):
        pass

    class _ExistingImage(RuntimeError):
        def __init__(self, existing_image):
            self.existing_image = existing_image

    requested_original_key, requested_thumb_key = _product_media_keys(
        product,
        sha,
    )
    attempt_token = uuid.uuid4().hex
    original_stem, original_suffix = requested_original_key.rsplit('.', 1)
    thumb_stem, thumb_suffix = requested_thumb_key.rsplit('.', 1)
    thumb_base = thumb_stem.removesuffix('_thumb')
    saved_keys = _save_product_image_pair(
        default_storage,
        f'{original_stem}-manual-{attempt_token}.{original_suffix}',
        original_bytes,
        f'{thumb_base}-manual-{attempt_token}_thumb.{thumb_suffix}',
        thumb_bytes,
    )
    if saved_keys is None:
        return None
    saved_original_key, saved_thumb_key = saved_keys

    for _attempt in range(3):
        try:
            with locked_product_images_feed_write(
                product.pk,
                bump=True,
            ) as (locked_product, images):
                existing = next(
                    (candidate for candidate in images.values() if candidate.sha256 == sha),
                    None,
                )
                if existing is not None:
                    raise _ExistingImage(existing)
                if sum(
                    candidate.status != ProductImage.Status.REJECTED
                    for candidate in images.values()
                ) >= MAX_PHOTOS:
                    raise _UploadAborted

                position = len(images)
                uploaded = ProductImage.objects.create(
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
            return uploaded
        except StaleProductFeedWrite:
            continue
        except _ExistingImage as exc:
            delete_storage_keys(saved_keys, storage=default_storage)
            return exc.existing_image
        except _UploadAborted:
            delete_storage_keys(saved_keys, storage=default_storage)
            return None
        except Exception:
            delete_storage_keys(saved_keys, storage=default_storage)
            raise
    delete_storage_keys(saved_keys, storage=default_storage)
    raise StaleProductFeedWrite(
        f'Product {product.pk} images changed repeatedly during upload.',
    )
