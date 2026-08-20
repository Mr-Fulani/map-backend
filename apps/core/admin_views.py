from datetime import timedelta

from django.contrib import admin
from django.db.models import Count
from django.shortcuts import render
from django.utils.timezone import now


def stats_view(request):
    """
    Кастомная Admin-страница со статистикой платформы MAP.

    Отображает: активные тенанты, листинги по статусам, ошибки за 24 часа,
    глубину очередей Celery.
    """
    from apps.marketplaces.models import Listing
    from apps.sync.models import SyncLog
    from apps.tenants.models import Tenant

    # Тенанты
    total_tenants = Tenant.objects.count()
    active_tenants = Tenant.objects.filter(is_active=True).count()

    # Листинги по статусам
    status_counts = dict(
        Listing.objects.values_list('status').annotate(total=Count('pk'))
    )
    listing_statuses = {
        status_label: status_counts.get(status_val, 0)
        for status_val, status_label in Listing.STATUS_CHOICES
    }

    # Ошибки за 24 часа
    errors_24h = SyncLog.objects.filter(
        status=SyncLog.STATUS_ERROR,
        created_at__gte=now() - timedelta(hours=24),
    ).count()

    warnings_24h = SyncLog.objects.filter(
        status=SyncLog.STATUS_WARN,
        created_at__gte=now() - timedelta(hours=24),
    ).count()

    # Snapshot собирается по расписанию. HTTP request не делает
    # Celery broadcast и не сканирует Redis broker.
    from apps.core.queue_observability import get_cached_celery_queue_snapshot
    queue_snapshot = get_cached_celery_queue_snapshot()

    context = {
        **admin.site.each_context(request),
        'title': 'Статистика платформы MAP',
        'total_tenants': total_tenants,
        'active_tenants': active_tenants,
        'listing_statuses': listing_statuses,
        'errors_24h': errors_24h,
        'warnings_24h': warnings_24h,
        'queue_snapshot': queue_snapshot,
        'now': now(),
    }
    return render(request, 'admin/stats.html', context)
