from datetime import timedelta

from django.conf import settings
from django.utils import timezone


def purge_retained_data(*, dry_run: bool = False) -> dict[str, int]:
    """Физически удаляет только данные, чей soft-delete/audit retention истёк."""
    from apps.billing.models import BillingWebhookEvent
    from apps.datasources.models import DataSourceConnection
    from apps.marketplaces.models import Listing, MarketplaceAccount
    from apps.products.models import Product
    from apps.sync.models import SyncLog
    from apps.tenants.models import WebhookDelivery, WebhookEndpoint, WebhookEvent

    now = timezone.now()
    soft_cutoff = now - timedelta(days=settings.SOFT_DELETE_RETENTION_DAYS)
    webhook_cutoff = now - timedelta(days=settings.WEBHOOK_AUDIT_RETENTION_DAYS)
    billing_cutoff = now - timedelta(days=settings.BILLING_AUDIT_RETENTION_DAYS)
    sync_cutoff = now - timedelta(days=settings.SYNC_LOG_RETENTION_DAYS)

    querysets = {
        'listings': Listing.all_objects.filter(deleted_at__lt=soft_cutoff),
        'products': Product.all_objects.filter(deleted_at__lt=soft_cutoff),
        'marketplace_accounts': MarketplaceAccount.all_objects.filter(deleted_at__lt=soft_cutoff),
        'datasource_connections': DataSourceConnection.all_objects.filter(deleted_at__lt=soft_cutoff),
        'webhook_endpoints': WebhookEndpoint.all_objects.filter(deleted_at__lt=soft_cutoff),
        'webhook_events': WebhookEvent.objects.filter(created_at__lt=webhook_cutoff).exclude(
            deliveries__status__in=[
                WebhookDelivery.STATUS_PENDING,
                WebhookDelivery.STATUS_RETRY,
                WebhookDelivery.STATUS_DELIVERING,
            ],
        ),
        'billing_webhook_events': BillingWebhookEvent.objects.filter(
            created_at__lt=billing_cutoff,
            processed_at__isnull=False,
        ).exclude(decision=BillingWebhookEvent.DECISION_MANUAL_REVIEW),
        'sync_logs': SyncLog.objects.filter(created_at__lt=sync_cutoff),
    }

    result = {name: queryset.count() for name, queryset in querysets.items()}
    if dry_run:
        return result
    # Сначала дочерние записи, затем их владельцы.
    for name in (
        'listings', 'products', 'marketplace_accounts', 'datasource_connections',
        'webhook_endpoints', 'webhook_events', 'billing_webhook_events', 'sync_logs',
    ):
        querysets[name].delete()
    return result
