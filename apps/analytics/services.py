"""Bounded, tenant-scoped aggregation for the main customer dashboard."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import Case, Count, IntegerField, Max, Q, Sum, Value, When
from django.db.models.functions import Substr
from django.utils import timezone

from apps.billing.ai_wallet import AIWalletService
from apps.billing.models import Subscription
from apps.datasources.models import DataSourceConnection
from apps.marketplaces.channel_listing_index import channel_status_counts
from apps.marketplaces.models import (
    AvitoAccountStatus,
    Listing,
    ListingStats,
    MarketplaceAccount,
)
from apps.marketplaces.serializers import AvitoAccountStatusSerializer
from apps.products.models import (
    Product,
    ProductCatalogClassification,
    ProductEnrichmentFact,
    ProductImage,
    ReviewStatus,
    VehicleFitment,
)
from apps.sync.models import SyncLog
from apps.web_research.models import WebResearchRun


ANALYTICS_PERIOD_DAYS = 30
MAX_ACTIVITY_ITEMS = 10
MAX_ACTIVITY_ISSUES = 7
MAX_ACTIVITY_SUCCESSES = MAX_ACTIVITY_ITEMS - MAX_ACTIVITY_ISSUES
ACTIVITY_WINDOW_DAYS = 7
MAX_DATASOURCE_ITEMS = 20
MAX_DATASOURCE_ISSUES = 5
MAX_AVITO_ACCOUNTS = 20
MAX_MESSAGE_LENGTH = 500


def _number(value: Any) -> int:
    return int(value or 0)


def _decimal_string(value: Any) -> str:
    return format(Decimal(value or 0), 'f')


def _attention(
    code: str,
    severity: str,
    title: str,
    message: str,
    *,
    count: int = 1,
    href: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        'code': code,
        'severity': severity,
        'title': title,
        'message': message,
        'count': count,
        'href': href,
        'metadata': metadata or {},
    }


def _subscription_and_usage(
    tenant,
    *,
    product_count: int,
    active_listing_count: int,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    try:
        subscription = tenant.subscription
    except Subscription.DoesNotExist:
        subscription = None

    today = timezone.localdate()
    status = subscription.effective_status if subscription else None
    current_period_days_left = None
    grace_days_left = None
    if subscription and status in {
        Subscription.STATUS_TRIAL,
        Subscription.STATUS_ACTIVE,
    }:
        current_period_days_left = max(
            0,
            (subscription.current_period_end - today).days,
        )
    elif subscription and status == Subscription.STATUS_PAST_DUE:
        elapsed = (today - subscription.current_period_end).days
        grace_days_left = max(0, settings.BILLING_GRACE_PERIOD_DAYS - elapsed)

    wallet = AIWalletService.read_only_summary(tenant)
    ai_usage = {
        'used': _decimal_string(wallet['included_used']),
        'successful_requests': int(tenant.ai_credits_used),
        'limit': _decimal_string(wallet['included_limit']),
        'included_balance': _decimal_string(wallet['included']),
        'included_percent_used': _decimal_string(wallet['included_percent_used']),
        'purchased_balance': _decimal_string(wallet['purchased']),
        'reserved_balance': _decimal_string(wallet['reserved']),
        'total_balance': _decimal_string(wallet['total']),
        'available_balance': _decimal_string(wallet['available']),
        'included_expires_at': (
            wallet['included_expires_at'].isoformat()
            if wallet['included_expires_at'] else None
        ),
        'unlimited': bool(wallet['unlimited']),
        'individual_limit': bool(wallet['individual_limit']),
        'overage_active': bool(wallet['overage_active']),
        'threshold': str(wallet['threshold']),
    }
    plan = subscription.plan if subscription else None
    usage = {
        'listings': {
            'used': active_listing_count,
            'limit': plan.limit_listings if plan else None,
        },
        'sku': {
            'used': product_count,
            'limit': plan.limit_sku if plan else None,
        },
        'ai_credits': ai_usage,
    }
    subscription_data = {
        'plan': plan.slug if plan else None,
        'status': status,
        'access_mode': subscription.access_mode if subscription else None,
        'current_period_end': (
            subscription.current_period_end.isoformat() if subscription else None
        ),
        'current_period_days_left': current_period_days_left,
        'grace_days_left': grace_days_left,
    }

    attention: list[dict[str, Any]] = []
    if subscription is None:
        attention.append(_attention(
            'subscription_missing',
            'critical',
            'Подписка не настроена',
            'Выберите тариф, чтобы использовать рабочие функции платформы.',
            href='/dashboard/billing',
        ))
    elif status in {Subscription.STATUS_PAST_DUE, Subscription.STATUS_CANCELLED}:
        attention.append(_attention(
            'subscription_inactive',
            'critical',
            'Подписка неактивна',
            'Оплатите тариф, чтобы снова запускать импорт, публикацию и AI-операции.',
            href='/dashboard/billing',
            metadata={'status': status, 'grace_days_left': grace_days_left},
        ))
    elif current_period_days_left is not None and current_period_days_left <= 7:
        attention.append(_attention(
            'subscription_renewal_due',
            'warning',
            'Скоро окончание периода',
            f'До окончания текущего периода осталось {current_period_days_left} дн.',
            href='/dashboard/billing',
            metadata={'days_left': current_period_days_left},
        ))

    threshold = str(wallet['threshold'])
    available = Decimal(wallet['available'])
    if subscription is not None and available <= 0:
        attention.append(_attention(
            'ai_credit_balance',
            'critical',
            'AI-баланс исчерпан',
            'Пополните баланс или дождитесь обновления включённого пакета.',
            href='/dashboard/billing#ai-credits',
            metadata={'threshold': threshold, 'overage_active': False},
        ))
    elif subscription is not None and wallet['overage_active']:
        attention.append(_attention(
            'ai_credit_balance',
            'info',
            'Используются купленные AI-кредиты',
            (
                'Включённый пакет закончился. '
                f"Доступно купленных кредитов: {ai_usage['available_balance']}."
            ),
            href='/dashboard/billing#ai-credits',
            metadata={'threshold': threshold, 'overage_active': True},
        ))
    elif subscription is not None and threshold in {'warning', 'critical'}:
        attention.append(_attention(
            'ai_credit_balance',
            'critical' if threshold == 'critical' else 'warning',
            'Заканчиваются AI-кредиты',
            f"Доступно AI-кредитов: {ai_usage['available_balance']}.",
            href='/dashboard/billing#ai-credits',
            metadata={'threshold': threshold, 'overage_active': False},
        ))
    return subscription_data, usage, attention


def _listing_counts(tenant) -> dict[str, int]:
    return channel_status_counts(tenant)


def _review_counts(tenant) -> dict[str, int]:
    common = {
        'tenant': tenant,
        'needs_review': True,
        'review_status': ReviewStatus.PENDING,
    }
    counts = {
        'fitments': VehicleFitment.objects.filter(**common).count(),
        'facts': ProductEnrichmentFact.objects.filter(**common).count(),
        'classifications': ProductCatalogClassification.objects.filter(**common).count(),
        'images': ProductImage.objects.filter(
            product__tenant=tenant,
            status=ProductImage.Status.NEEDS_REVIEW,
        ).count(),
    }
    counts['queue_total'] = counts['fitments'] + counts['facts'] + counts['classifications']
    return counts


def _research_counts(tenant) -> dict[str, int]:
    values = WebResearchRun.objects.filter(tenant=tenant).aggregate(
        total=Count('id'),
        active=Count('id', filter=Q(status__in=[
            WebResearchRun.Status.QUEUED,
            WebResearchRun.Status.RUNNING,
        ])),
        need_review=Count(
            'id', filter=Q(status=WebResearchRun.Status.NEED_REVIEW),
        ),
        failed=Count('id', filter=Q(status=WebResearchRun.Status.FAILED)),
    )
    return {key: _number(value) for key, value in values.items()}


def _analytics(tenant, *, active_listing_count: int) -> dict[str, Any]:
    date_to = timezone.localdate()
    date_from = date_to - timedelta(days=ANALYTICS_PERIOD_DAYS - 1)
    queryset = ListingStats.objects.filter(
        tenant=tenant,
        date__gte=date_from,
        date__lte=date_to,
    )
    totals = queryset.aggregate(
        views=Sum('views'),
        contacts=Sum('contacts'),
        impressions=Sum('impressions'),
    )
    views = _number(totals['views'])
    contacts = _number(totals['contacts'])
    impressions = _number(totals['impressions'])
    raw_daily = {
        row['date']: row
        for row in queryset.values('date').annotate(
            views=Sum('views'),
            contacts=Sum('contacts'),
            impressions=Sum('impressions'),
        ).order_by('date')
    }
    daily = []
    for day_offset in range(ANALYTICS_PERIOD_DAYS):
        day = date_from + timedelta(days=day_offset)
        row = raw_daily.get(day, {})
        daily.append({
            'date': day.isoformat(),
            'views': _number(row.get('views')),
            'contacts': _number(row.get('contacts')),
            'impressions': _number(row.get('impressions')),
        })
    return {
        'period_days': ANALYTICS_PERIOD_DAYS,
        'date_from': date_from.isoformat(),
        'date_to': date_to.isoformat(),
        'summary': {
            # Compatibility field names predate the Avito stats mapping:
            # views=uniqViews, impressions=all views, avg_ctr=unique share.
            'views': views,
            'contacts': contacts,
            'impressions': impressions,
            'avg_ctr': round(views / impressions * 100, 2) if impressions else 0.0,
            'active_listings': active_listing_count,
        },
        'daily': daily,
    }


def _datasources(tenant) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    queryset = DataSourceConnection.objects.filter(tenant=tenant)
    totals = queryset.aggregate(
        total=Count('id'),
        active=Count('id', filter=Q(is_active=True)),
        healthy=Count(
            'id',
            filter=Q(
                is_active=True,
                last_sync_status=DataSourceConnection.STATUS_OK,
            ),
        ),
        errors=Count(
            'id',
            filter=Q(
                is_active=True,
                last_sync_status=DataSourceConnection.STATUS_ERROR,
            ),
        ),
        never_synced=Count(
            'id',
            filter=Q(
                is_active=True,
                last_sync_status=DataSourceConnection.STATUS_NEVER,
            ),
        ),
        latest_sync_at=Max('last_sync_at'),
    )
    rows = list(
        queryset.annotate(
            _health_priority=Case(
                When(
                    is_active=True,
                    last_sync_status=DataSourceConnection.STATUS_ERROR,
                    then=Value(0),
                ),
                When(
                    is_active=True,
                    last_sync_status=DataSourceConnection.STATUS_NEVER,
                    then=Value(1),
                ),
                When(is_active=True, then=Value(2)),
                default=Value(3),
                output_field=IntegerField(),
            ),
            last_error_excerpt=Substr('last_error', 1, MAX_MESSAGE_LENGTH),
        ).order_by('_health_priority', '-last_sync_at', 'id').values(
            'id',
            'name',
            'type',
            'is_active',
            'last_sync_at',
            'last_sync_status',
            'last_error_excerpt',
        )[:MAX_DATASOURCE_ITEMS]
    )
    items = [
        {
            'id': row['id'],
            'name': row['name'],
            'type': row['type'],
            'is_active': row['is_active'],
            'last_sync_at': (
                row['last_sync_at'].isoformat() if row['last_sync_at'] else None
            ),
            'last_sync_status': row['last_sync_status'],
            'last_error': row['last_error_excerpt'] or '',
        }
        for row in rows
    ]
    latest_issues = [
        {
            'id': item['id'],
            'name': item['name'],
            'last_sync_at': item['last_sync_at'],
            'message': item['last_error'] or 'Последняя синхронизация завершилась ошибкой.',
        }
        for item in items
        if (
            item['is_active']
            and item['last_sync_status'] == DataSourceConnection.STATUS_ERROR
        )
    ][:MAX_DATASOURCE_ISSUES]
    total = _number(totals['total'])
    error_count = _number(totals['errors'])
    never_synced_count = _number(totals['never_synced'])
    data = {
        'total': total,
        'active': _number(totals['active']),
        'healthy': _number(totals['healthy']),
        'errors': error_count,
        'never_synced': never_synced_count,
        'latest_sync_at': (
            totals['latest_sync_at'].isoformat() if totals['latest_sync_at'] else None
        ),
        'returned_count': len(items),
        'truncated': total > len(items),
        'items': items,
        'latest_issues': latest_issues,
    }
    attention: list[dict[str, Any]] = []
    if total == 0:
        attention.append(_attention(
            'datasource_missing',
            'info',
            'Нет источника данных',
            'Подключите 1С или загрузите прайс-лист, чтобы наполнить каталог.',
            href='/dashboard/settings#datasources',
        ))
    if error_count:
        attention.append(_attention(
            'datasource_errors',
            'critical',
            'Ошибки источников данных',
            f'Источников с ошибкой последней синхронизации: {error_count}.',
            count=error_count,
            href='/dashboard/settings#datasources',
        ))
    if never_synced_count:
        attention.append(_attention(
            'datasource_never_synced',
            'info',
            'Источники ещё не синхронизировались',
            f'Ожидают первой синхронизации: {never_synced_count}.',
            count=never_synced_count,
            href='/dashboard/settings#datasources',
        ))
    return data, attention


def _activity_groups(tenant, *, statuses: list[str], limit: int) -> list[dict[str, Any]]:
    recent_since = timezone.now() - timedelta(days=ACTIVITY_WINDOW_DAYS)
    return list(
        SyncLog.objects.filter(
            tenant=tenant,
            status__in=statuses,
            created_at__gte=recent_since,
        ).annotate(
            message_excerpt=Substr('message', 1, MAX_MESSAGE_LENGTH),
        ).values(
            'event_type',
            'status',
            'message_excerpt',
        ).annotate(
            repeat_count=Count('id'),
            last_occurred_at=Max('created_at'),
            product_count=Count('product_id', distinct=True),
            product_owner_count=Count(
                'id', filter=Q(product_id__isnull=False),
            ),
            listing_count=Count('listing_id', distinct=True),
            listing_owner_count=Count(
                'id', filter=Q(listing_id__isnull=False),
            ),
            last_product_id=Max('product_id'),
            last_listing_id=Max('listing_id'),
        ).order_by('-last_occurred_at')[:limit]
    )


def _activity(tenant) -> list[dict[str, Any]]:
    event_labels = dict(SyncLog.EVENT_CHOICES)
    rows = _activity_groups(
        tenant,
        statuses=[SyncLog.STATUS_ERROR, SyncLog.STATUS_WARN],
        limit=MAX_ACTIVITY_ISSUES,
    ) + _activity_groups(
        tenant,
        statuses=[SyncLog.STATUS_OK],
        limit=MAX_ACTIVITY_SUCCESSES,
    )
    rows.sort(key=lambda row: row['last_occurred_at'], reverse=True)
    activity = []
    for row in rows[:MAX_ACTIVITY_ITEMS]:
        product_id = (
            row['last_product_id']
            if (
                row['product_count'] == 1
                and row['product_owner_count'] == row['repeat_count']
            )
            else None
        )
        listing_id = (
            row['last_listing_id']
            if (
                row['listing_count'] == 1
                and row['listing_owner_count'] == row['repeat_count']
            )
            else None
        )
        if product_id:
            href = f'/dashboard/products/{product_id}'
        elif listing_id:
            href = f'/dashboard/listings?listing={listing_id}'
        else:
            href = f"/dashboard/logs?status={row['status']}"
        activity.append({
            'code': row['event_type'],
            'severity': {
                SyncLog.STATUS_ERROR: 'error',
                SyncLog.STATUS_WARN: 'warning',
                SyncLog.STATUS_OK: 'success',
            }[row['status']],
            'title': event_labels.get(row['event_type'], row['event_type']),
            'message': row['message_excerpt'],
            'occurred_at': row['last_occurred_at'].isoformat(),
            'product_id': product_id,
            'listing_id': listing_id,
            'href': href,
            'metadata': {
                'event_type': row['event_type'],
                'status': row['status'],
                'repeat_count': row['repeat_count'],
                'window_days': ACTIVITY_WINDOW_DAYS,
                'aggregation': 'event_status_message_excerpt',
                'message_excerpt_length': MAX_MESSAGE_LENGTH,
            },
        })
    return activity


def _avito_warning(account_data: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    severity = 'info'
    messages: list[str] = []
    reasons: list[str] = []

    def add(reason: str, message: str, level: str = 'warning') -> None:
        nonlocal severity
        reasons.append(reason)
        messages.append(message)
        ranks = {'info': 0, 'warning': 1, 'critical': 2}
        if ranks[level] > ranks[severity]:
            severity = level

    if account_data['connection_status'] == AvitoAccountStatus.CONNECTION_AUTH_ERROR:
        add('auth_error', 'Avito отклонил ключи доступа.', 'critical')
    elif account_data['connection_status'] == AvitoAccountStatus.CONNECTION_UNAVAILABLE:
        add('connection_unavailable', 'Avito временно недоступен.')
    if account_data['autoload_status'] in {
        AvitoAccountStatus.AUTOLOAD_DISABLED,
        AvitoAccountStatus.AUTOLOAD_MISSING,
        AvitoAccountStatus.AUTOLOAD_FORBIDDEN,
    }:
        add('autoload_inactive', 'Автозагрузка не активирована.')
    if account_data['feed_configured'] is False:
        add('feed_not_configured', 'Фид MAP не настроен в Avito.')
    if account_data['tariff_status'] == AvitoAccountStatus.TARIFF_INACTIVE:
        add('tariff_inactive', 'Тариф Avito неактивен.', 'critical')
    elif account_data['tariff_status'] == AvitoAccountStatus.TARIFF_UNAVAILABLE:
        add('tariff_unavailable', 'Статус тарифа временно недоступен.')
    days_left = account_data['days_left']
    if days_left is not None and days_left <= 7:
        subscription_source = account_data['subscription_source']
        if subscription_source == 'avito_tariff':
            add('tariff_expiring', f'До окончания тарифа осталось {days_left} дн.')
        elif subscription_source == 'manual':
            raw_end = account_data['subscription_ends_at']
            try:
                manual_end = date.fromisoformat(raw_end) if raw_end else None
            except ValueError:
                manual_end = None
            if manual_end is not None and manual_end < timezone.localdate():
                add(
                    'manual_subscription_date_expired',
                    (
                        'Указанная вручную дата окончания '
                        f'Автозагрузки ({manual_end:%d.%m.%Y}) уже прошла. '
                        'Проверьте актуальный срок в Avito.'
                    ),
                )
            elif days_left == 0:
                add(
                    'manual_subscription_date_expiring',
                    'Указанная вручную дата окончания '
                    'Автозагрузки — сегодня.',
                )
            else:
                add(
                    'manual_subscription_date_expiring',
                    'По указанной вручную дате до окончания '
                    f'Автозагрузки осталось {days_left} дн.',
                )
    remaining = account_data['placements_remaining']
    total = account_data['placements_total']
    if remaining is not None and total and remaining / total <= 0.2:
        add('placements_low', f'Осталось {remaining} размещений из {total}.')
    if account_data['last_error_message'] and not reasons:
        add('status_check_error', account_data['last_error_message'])
    if (
        account_data['profile_stale']
        and account_data['connection_status'] != AvitoAccountStatus.CONNECTION_UNKNOWN
    ):
        add('profile_stale', 'Данные профиля Avito не обновлялись более 12 часов.')
    if (
        account_data['tariff_stale']
        and account_data['tariff_status'] != AvitoAccountStatus.TARIFF_UNKNOWN
    ):
        add('tariff_stale', 'Данные тарифа Avito не обновлялись более 12 часов.')
    return severity, messages, reasons


def _marketplaces(tenant) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    totals = MarketplaceAccount.objects.filter(tenant=tenant).aggregate(
        avito=Count(
            'id',
            filter=Q(marketplace=MarketplaceAccount.MARKETPLACE_AVITO),
        ),
        ozon=Count(
            'id',
            filter=Q(marketplace=MarketplaceAccount.MARKETPLACE_OZON),
        ),
    )
    queryset = MarketplaceAccount.objects.filter(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
    ).select_related('avito_status').order_by('-is_active', 'id')
    total = _number(totals['avito'])
    ozon_total = _number(totals['ozon'])
    accounts = list(queryset[:MAX_AVITO_ACCOUNTS])
    data = []
    attention = []
    for account in accounts:
        try:
            status_obj = account.avito_status
        except AvitoAccountStatus.DoesNotExist:
            status_obj = AvitoAccountStatus(account=account, tenant=tenant)
        status_data = dict(AvitoAccountStatusSerializer(status_obj).data)
        account_data = {
            'account_id': account.pk,
            'account_name': account.name,
            'is_active': account.is_active,
            'connection_status': status_data['connection_status'],
            'autoload_status': status_data['autoload_status'],
            'feed_configured': status_data['feed_configured'],
            'profile_stale': status_data['profile_stale'],
            'tariff_status': status_data['tariff_status'],
            'tariff_stale': status_data['tariff_stale'],
            'subscription_ends_at': status_data['subscription_ends_at'],
            'subscription_source': status_data['subscription_source'],
            'days_left': status_data['days_left'],
            'placements_remaining': status_data['placements_remaining'],
            'placements_total': status_data['placements_total'],
            'last_error_code': status_data['last_error_code'],
            'last_error_message': status_data['last_error_message'],
        }
        data.append(account_data)
        if not account.is_active:
            continue
        severity, messages, reasons = _avito_warning(account_data)
        if messages:
            attention.append(_attention(
                'avito_account_health',
                severity,
                f'Требуется внимание к Avito: {account.name}',
                ' '.join(messages[:3]),
                count=len(reasons),
                href='/dashboard/settings#marketplaces',
                metadata={'account_id': account.pk, 'reasons': reasons},
            ))
    if total > len(data):
        attention.append(_attention(
            'avito_accounts_truncated',
            'info',
            'Показана часть аккаунтов Avito',
            (
                f'На дашборде проверены {len(data)} из {total} аккаунтов. '
                'Откройте настройки, чтобы увидеть остальные.'
            ),
            count=total - len(data),
            href='/dashboard/settings#marketplaces',
            metadata={'returned_count': len(data), 'total': total},
        ))
    return {
        'avito_total': total,
        'ozon_total': ozon_total,
        'avito_truncated': total > len(data),
        'avito': data,
    }, attention


def build_tenant_dashboard_summary(tenant) -> dict[str, Any]:
    """Build a display-ready summary using a fixed number of bounded queries."""
    product_count = Product.objects.filter(tenant=tenant).count()
    listings = _listing_counts(tenant)
    # Billing charges every active listing, including historical listings on a
    # marketplace account that has since been disabled.  Keep quota usage in
    # sync with BillingService while the actionable funnel stays scoped to the
    # active accounts exposed by the Listings page.
    quota_active_listing_count = Listing.objects.filter(
        tenant=tenant,
        status=Listing.STATUS_ACTIVE,
    ).count()
    subscription, usage, attention = _subscription_and_usage(
        tenant,
        product_count=product_count,
        active_listing_count=quota_active_listing_count,
    )

    reviews = _review_counts(tenant)
    if reviews['queue_total']:
        attention.append(_attention(
            'review_queue',
            'warning',
            'Данные ждут проверки',
            f"В очереди проверки: {reviews['queue_total']}.",
            count=reviews['queue_total'],
            href='/dashboard/review',
            metadata={
                'fitments': reviews['fitments'],
                'facts': reviews['facts'],
                'classifications': reviews['classifications'],
            },
        ))
    if reviews['images']:
        attention.append(_attention(
            'image_review',
            'warning',
            'Изображения ждут проверки',
            f"Нужно проверить изображений: {reviews['images']}.",
            count=reviews['images'],
            href='/dashboard/products',
        ))

    research = _research_counts(tenant)
    if research['active']:
        attention.append(_attention(
            'research_active',
            'info',
            'Исследования выполняются',
            f"Активных запусков: {research['active']}.",
            count=research['active'],
            href='/dashboard/research',
        ))
    if research['need_review']:
        attention.append(_attention(
            'research_review',
            'warning',
            'Результаты исследований ждут проверки',
            f"Запусков на проверке: {research['need_review']}.",
            count=research['need_review'],
            href='/dashboard/research',
        ))
    if research['failed']:
        attention.append(_attention(
            'research_failed',
            'critical',
            'Ошибки интернет-исследований',
            f"Запусков с ошибкой: {research['failed']}.",
            count=research['failed'],
            href='/dashboard/research',
        ))

    listing_alerts = [
        (
            'listing_requires_review', 'warning', 'Объявления ждут проверки',
            'requires_review', 'Требуют проверки',
        ),
        (
            'listing_rejected', 'critical', 'Площадки отклонили объявления',
            'rejected', 'Отклонено',
        ),
        (
            'listing_limit_reached', 'critical', 'Достигнут лимит размещений',
            'limit_reached', 'Не размещено из-за лимита',
        ),
    ]
    for code, severity, title, key, message in listing_alerts:
        count = listings[key]
        if count:
            attention.append(_attention(
                code,
                severity,
                title,
                f'{message}: {count}.',
                count=count,
                href=f'/dashboard/listings?status={key}',
                metadata={'status': key},
            ))

    datasources, datasource_attention = _datasources(tenant)
    marketplaces, marketplace_attention = _marketplaces(tenant)
    attention.extend(datasource_attention)
    attention.extend(marketplace_attention)
    severity_order = {'critical': 0, 'warning': 1, 'info': 2}
    attention.sort(key=lambda item: (severity_order[item['severity']], item['code']))

    return {
        'generated_at': timezone.now().isoformat(),
        'subscription': subscription,
        'usage': usage,
        'attention': attention,
        'analytics': _analytics(
            tenant,
            active_listing_count=listings['avito_active'],
        ),
        'funnel': {
            'products': product_count,
            'listings': listings['total'],
            'active_listings': listings['active'],
            'queued_listings': listings['queued'],
            'pending_listings': listings['pending'],
            'rejected_listings': listings['rejected'],
            'requires_review_listings': listings['requires_review'],
            'limit_reached_listings': listings['limit_reached'],
        },
        'activity': _activity(tenant),
        'datasources': datasources,
        'marketplaces': marketplaces,
        'services': {
            'image_processing': {
                'available': False,
                'status': 'coming_soon',
                'used': None,
                'limit': None,
                'unit': 'ai_credits',
                'title': 'AI-обработка изображений',
                'description': (
                    'Подготовка изображений будет расходовать общий AI-баланс. '
                    'Отдельная статистика сервиса пока недоступна.'
                ),
                'uses_shared_ai_balance': True,
                'href': '/dashboard/media',
                'metadata': {
                    'billing_model': 'shared_ai_balance',
                    'usage_reporting': 'not_available_yet',
                },
            },
        },
    }
