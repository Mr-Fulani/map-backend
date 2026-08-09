from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.billing.models import Invoice
from apps.marketplaces.models import Listing
from apps.sync.models import SyncLog
from apps.tenants.webhooks import enqueue_webhook_event


def _remember_old_status(sender, instance) -> None:
    if not instance.pk:
        instance._webhook_old_status = None
        return
    manager = getattr(sender, 'all_objects', sender.objects)
    instance._webhook_old_status = manager.filter(pk=instance.pk).values_list(
        'status', flat=True,
    ).first()


@receiver(pre_save, sender=Listing)
@receiver(pre_save, sender=Invoice)
def remember_status_before_save(sender, instance, **kwargs):
    _remember_old_status(sender, instance)


@receiver(post_save, sender=Listing)
def emit_listing_status_event(sender, instance, raw=False, **kwargs):
    if raw or getattr(instance, '_webhook_old_status', None) == instance.status:
        return
    event_map = {
        Listing.STATUS_ACTIVE: 'listing.published',
        Listing.STATUS_REJECTED: 'listing.rejected',
        Listing.STATUS_ARCHIVED: 'listing.archived',
        Listing.STATUS_DELETED: 'listing.archived',
    }
    event_type = event_map.get(instance.status)
    if event_type is None:
        return
    payload = {
        'listing_id': instance.pk,
        'product_id': instance.product_id,
        'account_id': instance.account_id,
        'external_id': instance.external_id,
        'status': instance.status,
        'rejection_reason': instance.rejection_reason,
    }
    idempotency_key = f'listing:{instance.pk}:{instance.status}:{instance.updated_at.isoformat()}'
    transaction.on_commit(lambda: enqueue_webhook_event(
        instance.tenant,
        event_type,
        payload,
        idempotency_key=idempotency_key,
    ))


@receiver(post_save, sender=Invoice)
def emit_invoice_status_event(sender, instance, raw=False, **kwargs):
    if raw or getattr(instance, '_webhook_old_status', None) == instance.status:
        return
    event_map = {
        Invoice.STATUS_PAID: 'billing.payment_success',
        Invoice.STATUS_FAILED: 'billing.payment_failed',
    }
    event_type = event_map.get(instance.status)
    if event_type is None:
        return
    payload = {
        'invoice_id': instance.pk,
        'purchase_type': instance.purchase_type,
        'amount': str(instance.amount),
        'currency': instance.currency,
        'status': instance.status,
    }
    idempotency_key = f'invoice:{instance.pk}:{instance.status}:{instance.updated_at.isoformat()}'
    transaction.on_commit(lambda: enqueue_webhook_event(
        instance.tenant,
        event_type,
        payload,
        idempotency_key=idempotency_key,
    ))


@receiver(post_save, sender=SyncLog)
def emit_import_event(sender, instance, created=False, raw=False, **kwargs):
    if raw or not created or instance.event_type != SyncLog.EVENT_DATASOURCE_IMPORT:
        return
    event_map = {
        SyncLog.STATUS_OK: 'import.completed',
        SyncLog.STATUS_ERROR: 'import.failed',
    }
    event_type = event_map.get(instance.status)
    if event_type is None:
        return
    payload = {
        'sync_log_id': instance.pk,
        'status': instance.status,
        'message': instance.message,
        'details': instance.payload,
    }
    transaction.on_commit(lambda: enqueue_webhook_event(
        instance.tenant,
        event_type,
        payload,
        idempotency_key=f'sync-log:{instance.pk}',
    ))
