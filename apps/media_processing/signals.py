from django.core.files.storage import default_storage
from django.db.models.signals import post_delete
from django.dispatch import receiver

from apps.media_processing.models import ProductImageVariant


@receiver(post_delete, sender=ProductImageVariant)
def delete_variant_file(sender, instance, **kwargs):
    """Remove an immutable derived object after its database record is deleted."""
    if instance.s3_key and default_storage.exists(instance.s3_key):
        default_storage.delete(instance.s3_key)
