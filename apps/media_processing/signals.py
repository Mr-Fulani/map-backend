from django.db.models import Model
from django.db.models.deletion import ProtectedError
from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver

from apps.core.storage import delete_storage_keys_on_commit
from apps.media_processing.models import MediaProcessingJob, ProductImageVariant
from apps.media_processing.protection import unresolved_media_job_q


def _raise_for_unresolved_jobs(queryset, *, object_label: str) -> None:
    protected_jobs: set[Model] = set(queryset.only('pk', 'status')[:100])
    if protected_jobs:
        raise ProtectedError(
            f'Cannot physically delete {object_label} while a media provider '
            'operation or credit reservation is unresolved.',
            protected_jobs,
        )


@receiver(pre_delete, sender=MediaProcessingJob)
def protect_unresolved_media_job(sender, instance, using, **kwargs):
    """Keep the only provider/accounting evidence until it is reconciled."""
    jobs = sender.objects.using(using).select_for_update().filter(
        pk=instance.pk,
    ).filter(unresolved_media_job_q())
    _raise_for_unresolved_jobs(jobs, object_label=f'media job {instance.pk}')


@receiver(pre_delete, sender='products.Product')
def protect_product_with_unresolved_media(sender, instance, using, **kwargs):
    """Keep provider evidence and held credits across every hard-delete path."""
    image_ids = list(
        instance.images.using(using).select_for_update().values_list('pk', flat=True),
    )
    if not image_ids:
        return
    jobs = MediaProcessingJob.objects.using(using).filter(
        product_image_id__in=image_ids,
    ).filter(unresolved_media_job_q())
    _raise_for_unresolved_jobs(jobs, object_label=f'product {instance.pk}')


@receiver(pre_delete, sender='products.ProductImage')
def protect_product_image_with_unresolved_media(sender, instance, using, **kwargs):
    """Block direct/queryset deletes as well as Product/Tenant cascades."""
    sender.objects.using(using).select_for_update().filter(pk=instance.pk).exists()
    jobs = MediaProcessingJob.objects.using(using).filter(
        product_image_id=instance.pk,
    ).filter(unresolved_media_job_q())
    _raise_for_unresolved_jobs(jobs, object_label=f'product image {instance.pk}')


@receiver(post_delete, sender=ProductImageVariant)
def delete_variant_file(sender, instance, using, **kwargs):
    """Remove an immutable derived object after its DB delete is committed."""
    delete_storage_keys_on_commit((instance.s3_key,), using=using)
