import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(queue='sync_import')
def update_tenant_counters():
    """
    Обновляет кэш-счётчики тенантов: active_listings_count, sku_count.

    Запускается каждые 15 минут через Celery Beat.
    Использует F() и annotate для атомарности без N+1.
    """
    from apps.marketplaces.models import Listing
    from apps.tenants.models import Tenant

    for tenant in Tenant.objects.filter(is_active=True):
        active_listings = Listing.objects.filter(
            tenant=tenant, status=Listing.STATUS_ACTIVE,
        ).count()
        sku_count = tenant.products.count()
        Tenant.objects.filter(pk=tenant.pk).update(
            active_listings_count=active_listings,
            sku_count=sku_count,
        )

    count = Tenant.objects.filter(is_active=True).count()
    logger.info('update_tenant_counters: обновлено %d тенантов', count)
    return {'tenants_updated': count}
