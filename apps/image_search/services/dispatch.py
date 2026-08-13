"""Durable scheduling helpers for product image search."""

import uuid

from django.db import transaction

from apps.core.dispatch import enqueue_durable_task
from apps.image_search.models import ImageSearchTask


def create_image_search_task(*, tenant, product, intent=None) -> ImageSearchTask:
    """Create the tenant-visible task and its dispatch atomically."""
    with transaction.atomic():
        task_id = str(uuid.uuid4())
        tracking = ImageSearchTask.objects.create(
            tenant=tenant,
            product=product,
            task_id=task_id,
            intent=intent,
        )
        dispatch = enqueue_durable_task(
            'apps.image_search.tasks.search_images_for_product',
            # The tracking row is also the stable paid-provider workflow owner.
            # It must reach every retry; a product ID alone cannot distinguish
            # a retry from a new tenant intent.
            args=[product.pk, tracking.pk],
            deduplication_key=f'image-search-request:{task_id}',
        )
        tracking.dispatch = dispatch
        tracking.save(update_fields=['dispatch', 'updated_at'])
    return tracking
