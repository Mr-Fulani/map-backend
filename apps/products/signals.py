from django.db.models.deletion import ProtectedError
from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver

from apps.core.storage import delete_storage_keys_on_commit
from apps.products.models import (
    ProductImage,
    ProductParseIntent,
    ProductParseJob,
    TenantCatalogCategory,
)


def _active_parse_workflows(job_ids, *, using):
    from apps.web_research.models import WebSearchWorkflow

    keys = [f'product-parse-job:{job_id}' for job_id in job_ids]
    return WebSearchWorkflow.objects.using(using).select_for_update().filter(
        operation='euroauto',
        workflow_key__in=keys,
        status__in=WebSearchWorkflow.ACTIVE_STATUSES,
    )


@receiver(pre_delete, sender=ProductParseJob)
def protect_parse_job_with_active_paid_workflow(sender, instance, using, **kwargs):
    """Keep the persisted owner required to replay a paid search checkpoint."""
    from apps.tenants.models import Tenant

    Tenant.objects.using(using).select_for_update().only('pk').get(
        pk=instance.tenant_id,
    )
    sender.objects.using(using).select_for_update().only('pk').get(pk=instance.pk)
    workflows = _active_parse_workflows([instance.pk], using=using)
    if workflows.exists():
        raise ProtectedError(
            'Product parse job has an active paid provider workflow.',
            set(workflows[:10]),
        )


@receiver(pre_delete, sender=ProductParseIntent)
def protect_parse_intent_with_active_paid_workflow(
    sender,
    instance,
    using,
    **kwargs,
):
    """A cascade may not erase a parse job that owns paid evidence."""
    from apps.tenants.models import Tenant

    Tenant.objects.using(using).select_for_update().only('pk').get(
        pk=instance.tenant_id,
    )
    sender.objects.using(using).select_for_update().only('pk').get(pk=instance.pk)
    job_ids = list(
        instance.jobs.using(using).select_for_update().values_list('pk', flat=True)
    )
    workflows = _active_parse_workflows(job_ids, using=using)
    if workflows.exists():
        raise ProtectedError(
            'Product parse intent owns an active paid provider workflow.',
            set(workflows[:10]),
        )


@receiver(post_delete, sender=ProductImage)
def delete_product_image_files(sender, instance, using, **kwargs):
    """Remove product media only after its database row is committed deleted."""
    delete_storage_keys_on_commit((
        instance.s3_key,
        instance.s3_key_preview,
        instance.s3_key_thumb,
    ), using=using)


@receiver(post_delete, sender=TenantCatalogCategory)
def delete_catalog_category_image(sender, instance, using, **kwargs):
    """Do not leave the category fallback behind after a hard delete."""
    delete_storage_keys_on_commit((instance.default_image_s3_key,), using=using)
