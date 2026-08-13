from datetime import timedelta

from django.contrib import admin
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
    listing_statuses = {}
    for status_val, status_label in Listing.STATUS_CHOICES:
        listing_statuses[status_label] = Listing.objects.filter(status=status_val).count()

    # Ошибки за 24 часа
    errors_24h = SyncLog.objects.filter(
        status=SyncLog.STATUS_ERROR,
        created_at__gte=now() - timedelta(hours=24),
    ).count()

    warnings_24h = SyncLog.objects.filter(
        status=SyncLog.STATUS_WARN,
        created_at__gte=now() - timedelta(hours=24),
    ).count()

    # Глубина очередей Celery
    queue_depths = _get_celery_queue_depths()

    context = {
        **admin.site.each_context(request),
        'title': 'Статистика платформы MAP',
        'total_tenants': total_tenants,
        'active_tenants': active_tenants,
        'listing_statuses': listing_statuses,
        'errors_24h': errors_24h,
        'warnings_24h': warnings_24h,
        'queue_depths': queue_depths,
        'now': now(),
    }
    return render(request, 'admin/stats.html', context)


def _get_celery_queue_depths() -> dict:
    """Возвращает глубину очередей через Celery inspect с таймаутом 2 сек."""
    try:
        from celery import current_app

        inspect = current_app.control.inspect(timeout=2)
        reserved = inspect.reserved() or {}

        counts: dict[str, int] = {}
        for worker_tasks in reserved.values():
            for task in worker_tasks:
                queue = task.get('delivery_info', {}).get('routing_key', 'unknown')
                counts[queue] = counts.get(queue, 0) + 1
        return counts
    except Exception:
        return {}
