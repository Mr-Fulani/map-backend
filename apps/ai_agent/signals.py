"""Model-level guards for unresolved paid AI provider state."""

from django.db.models.deletion import ProtectedError
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from apps.ai_agent.models import AIProviderOperation
from apps.ai_agent.protection import (
    is_unresolved_ai_provider_operation,
    unresolved_ai_provider_operation_q,
)
from apps.tenants.models import Tenant


@receiver(pre_delete, sender=AIProviderOperation)
def protect_unresolved_ai_provider_operation(
    sender,
    instance,
    using,
    **kwargs,
):
    """ORM/admin/queryset deletes cannot erase held or unapplied accounting."""
    locked = sender.objects.using(using).select_for_update().get(pk=instance.pk)
    if is_unresolved_ai_provider_operation(locked):
        raise ProtectedError(
            'Unresolved AI provider operation cannot be deleted.',
            {locked},
        )


@receiver(pre_delete, sender='products.Product')
def protect_product_with_unresolved_ai_operation(sender, instance, using, **kwargs):
    Tenant.objects.using(using).select_for_update().only('pk').get(
        pk=instance.tenant_id,
    )
    sender.all_objects.using(using).select_for_update().only('pk').get(
        pk=instance.pk,
    )
    operations = AIProviderOperation.objects.using(using).select_for_update().filter(
        unresolved_ai_provider_operation_q(),
        tenant_id=instance.tenant_id,
        domain_type=AIProviderOperation.DomainType.PRODUCT,
        domain_reference=str(instance.pk),
    )
    if operations.exists():
        raise ProtectedError(
            'Product has an unresolved AI provider operation.',
            set(operations[:10]),
        )


@receiver(pre_delete, sender='web_research.WebResearchRun')
def protect_web_run_with_unresolved_ai_operation(sender, instance, using, **kwargs):
    Tenant.objects.using(using).select_for_update().only('pk').get(
        pk=instance.tenant_id,
    )
    sender.objects.using(using).select_for_update().only('pk').get(pk=instance.pk)
    operations = AIProviderOperation.objects.using(using).select_for_update().filter(
        unresolved_ai_provider_operation_q(),
        tenant_id=instance.tenant_id,
        domain_type=AIProviderOperation.DomainType.WEB_RESEARCH_RUN,
        domain_reference=str(instance.pk),
    )
    if operations.exists():
        raise ProtectedError(
            'Web research run has an unresolved AI provider operation.',
            set(operations[:10]),
        )
