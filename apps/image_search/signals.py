import logging

from django.db.models.signals import post_delete
from django.dispatch import receiver

from apps.products.models import ProductImage

logger = logging.getLogger(__name__)


@receiver(post_delete, sender=ProductImage)
def delete_image_from_s3(sender, instance, **kwargs):
    """Удаляет все версии файла из S3 при удалении ProductImage.

    Срабатывает автоматически при:
    - Удалении через Admin (включая bulk delete, если переопределён delete_queryset)
    - Каскадном удалении Product
    - Прямом вызове instance.delete()
    """
    from django.core.files.storage import default_storage

    for key_field in ('s3_key', 's3_key_preview', 's3_key_thumb'):
        key = getattr(instance, key_field, '')
        if key:
            try:
                default_storage.delete(key)
            except Exception:
                logger.warning(f'S3 delete failed: {key}', exc_info=True)
