"""Signal receivers owned by image-search live in this module.

Product media lifecycle receivers are registered by ``apps.products`` because
those files must also be cleaned when image-search is disabled or removed.
"""

from django.db.models.deletion import ProtectedError
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from apps.image_search.models import ImageSearchIntent, ImageSearchTask


def _active_workflow_queryset_for_task_ids(task_ids, *, using):
    from apps.web_research.models import WebSearchWorkflow

    keys = [f'image-search-task:{task_id}' for task_id in task_ids]
    return WebSearchWorkflow.objects.using(using).select_for_update().filter(
        workflow_key__in=keys,
        operation='image_search',
        status__in=WebSearchWorkflow.ACTIVE_STATUSES,
    )


@receiver(pre_delete, sender=ImageSearchTask)
def protect_image_task_with_active_paid_workflow(sender, instance, using, **kwargs):
    from apps.tenants.models import Tenant

    Tenant.objects.using(using).select_for_update().only('pk').get(
        pk=instance.tenant_id,
    )
    sender.objects.using(using).select_for_update().only('pk').get(pk=instance.pk)
    workflows = _active_workflow_queryset_for_task_ids(
        [instance.pk],
        using=using,
    )
    if workflows.exists():
        raise ProtectedError(
            'Image search task has an active paid provider workflow.',
            set(workflows[:10]),
        )


@receiver(pre_delete, sender=ImageSearchIntent)
def protect_image_intent_with_active_paid_workflow(sender, instance, using, **kwargs):
    from apps.tenants.models import Tenant

    Tenant.objects.using(using).select_for_update().only('pk').get(
        pk=instance.tenant_id,
    )
    task_ids = list(
        instance.tasks.using(using).select_for_update().values_list('pk', flat=True)
    )
    workflows = _active_workflow_queryset_for_task_ids(
        task_ids,
        using=using,
    )
    if workflows.exists():
        raise ProtectedError(
            'Image search intent owns an active paid provider workflow.',
            set(workflows[:10]),
        )
