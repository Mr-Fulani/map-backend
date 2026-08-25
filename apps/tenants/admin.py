from django.contrib import admin, messages
from django.utils import timezone
from django.utils.html import format_html
from unfold.admin import ModelAdmin

from apps.tenants.models import (
    APIKey, CatalogDomain, Tenant, TenantCatalogDomain, TenantUser,
    WebhookDelivery, WebhookEndpoint, WebhookEvent,
)


@admin.register(CatalogDomain)
class CatalogDomainAdmin(ModelAdmin):
    """Управление platform-level доменами каталога."""

    list_display = [
        'name', 'slug', 'short_name', 'is_active', 'is_system',
        'supports_auto_parts_enrichment', 'requires_product_classification', 'sort_order',
    ]
    list_filter = [
        'is_active', 'is_system', 'supports_auto_parts_enrichment',
        'requires_product_classification',
    ]
    search_fields = ['name', 'short_name', 'slug', 'seo_title', 'seo_keywords']
    prepopulated_fields = {'slug': ('name',)}
    fieldsets = [
        ('Основное', {
            'fields': [
                'name', 'short_name', 'slug', 'description', 'is_active',
                'is_system', 'sort_order',
            ],
        }),
        ('Возможности платформы', {
            'fields': ['supports_auto_parts_enrichment', 'requires_product_classification'],
        }),
        ('SEO', {
            'fields': [
                'seo_title', 'seo_description', 'seo_keywords', 'seo_h1',
                'canonical_path', 'meta_robots',
            ],
        }),
        ('Open Graph', {
            'fields': ['og_title', 'og_description', 'og_image_url'],
            'classes': ['collapse'],
        }),
    ]


class TenantUserInline(admin.TabularInline):
    """Инлайн пользователей тенанта в карточке тенанта."""

    model = TenantUser
    extra = 0
    readonly_fields = ['user', 'get_phone', 'role', 'created_at']
    can_delete = False

    @admin.display(description='Телефон')
    def get_phone(self, obj):
        """Возвращает телефон пользователя из связанной модели User."""
        return obj.user.phone or '—'


class TenantCatalogDomainInline(admin.TabularInline):
    model = TenantCatalogDomain
    extra = 0
    autocomplete_fields = ['domain']
    fields = ['domain', 'is_enabled', 'created_at']
    readonly_fields = ['created_at']


@admin.register(Tenant)
class TenantAdmin(ModelAdmin):
    """
    Администрирование тенантов.

    Показывает имя, план подписки, статус триала и счётчики использования.
    """

    list_display = [
        'name', 'slug', 'get_enabled_domains', 'is_active', 'get_plan',
        'get_access_status', 'active_listings_count', 'sku_count',
        'ai_credit_limit_override', 'created_at',
    ]
    list_filter = ['is_active']
    search_fields = ['name', 'slug']
    readonly_fields = [
        'get_owner_phone', 'get_telegram', 'get_subscription_info', 'get_enabled_domains',
        'get_sku_count', 'get_active_listings_count', 'ai_credits_used', 'get_brave_quota',
        'is_active', 'created_at', 'updated_at',
    ]
    actions = ['extend_trial_14_days']
    inlines = [TenantCatalogDomainInline, TenantUserInline]
    fieldsets = [
        ('Основное', {
            'fields': ['name', 'slug', 'get_enabled_domains', 'is_active'],
        }),
        ('Владелец', {
            'fields': ['get_owner_phone', 'get_telegram'],
            'description': 'Email владельца — в инлайне пользователей ниже.',
        }),
        ('Подписка', {
            'fields': ['get_subscription_info', 'ai_credit_limit_override'],
            'description': (
                'Управление подпиской — в разделе Биллинг → Подписки. '
                'Пустой AI-лимит использует значение тарифного плана.'
            ),
        }),
        ('Счётчики', {
            'fields': ['get_sku_count', 'get_active_listings_count', 'ai_credits_used', 'get_brave_quota'],
            'classes': ['collapse'],
        }),
        ('Служебное', {
            'fields': ['created_at', 'updated_at'],
            'classes': ['collapse'],
        }),
    ]

    def has_delete_permission(self, request, obj=None):
        """Tenant removal requires a reviewed lifecycle workflow."""

        return False

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change and 'ai_credit_limit_override' in form.changed_data:
            from apps.billing.ai_wallet import AIWalletService
            AIWalletService.sync_included_limit(obj)

    @admin.display(description='Brave запросов (месяц, платформа)')
    def get_brave_quota(self, obj):
        """Показывает использование Brave Search API за текущий месяц (платформенный лимит)."""
        from apps.image_search.models import BraveQuota
        quota = BraveQuota.objects.filter(period=BraveQuota.current().period).first()
        if not quota:
            return '0 / 800'
        return f'{quota.requests_used} / {BraveQuota.SOFT_CAP}'

    @admin.display(description='SKU (товаров)')
    def get_sku_count(self, obj):
        """Считает количество товаров тенанта напрямую из БД."""
        return obj.products.count()

    @admin.display(description='Активных листингов')
    def get_active_listings_count(self, obj):
        """Считает активные листинги тенанта напрямую из БД."""
        from apps.marketplaces.models import Listing
        return Listing.objects.filter(tenant=obj, status=Listing.STATUS_ACTIVE).count()

    @admin.display(description='Домены каталога')
    def get_enabled_domains(self, obj):
        """Возвращает список включённых доменов каталога из TenantCatalogDomain."""
        names = list(
            obj.enabled_catalog_domains
            .filter(is_enabled=True)
            .select_related('domain')
            .values_list('domain__name', flat=True)
            .order_by('domain__sort_order', 'domain__name')
        )
        return ', '.join(names) if names else '—'

    @admin.display(description='Тариф')
    def get_plan(self, obj):
        """Возвращает название тарифного плана тенанта."""
        try:
            return obj.subscription.plan.name
        except Exception:
            return '—'

    @admin.display(description='Доступ')
    def get_access_status(self, obj):
        """Показывает эффективный доступ, не зависящий от запуска Celery Beat."""
        try:
            sub = obj.subscription
        except Exception:
            return format_html('<span style="color:#ef4444;font-weight:600">Нет подписки</span>')

        if not sub.is_active:
            return format_html(
                '<span style="color:#ef4444;font-weight:600">Только чтение/оплата</span>'
            )

        today = timezone.localdate()
        days = max(0, (sub.current_period_end - today).days)
        label = 'Триал' if sub.effective_status == sub.STATUS_TRIAL else 'Оплачено'

        if days <= 3:
            return format_html(
                '<span style="color:#f97316;font-weight:600">{} · {} дн.</span>',
                label, days,
            )
        return format_html(
            '<span style="color:#22c55e;font-weight:600">{} · {} дн.</span>',
            label, days,
        )

    @admin.display(description='Телефон владельца')
    def get_owner_phone(self, obj):
        """Возвращает телефон владельца тенанта (роль owner)."""
        membership = obj.members.filter(role='owner').select_related('user').first()
        if membership:
            return membership.user.phone or '—'
        return '—'

    @admin.display(description='Telegram')
    def get_telegram(self, obj):
        """Возвращает Telegram username привязанный тенантом для уведомлений."""
        try:
            settings = obj.notification_settings
            if settings.telegram_chat_id:
                username = settings.telegram_username
                return f'@{username}' if username else f'chat_id: {settings.telegram_chat_id}'
        except Exception:
            pass
        return '—'

    @admin.display(description='Подписка')
    def get_subscription_info(self, obj):
        """Показывает статус и срок подписки одной строкой."""
        try:
            sub = obj.subscription
            end = sub.current_period_end
            end_str = end.strftime('%d.%m.%Y') if end else '∞'
            effective_label = dict(sub.STATUS_CHOICES)[sub.effective_status]
            return f'{sub.plan.name} / {effective_label} / до {end_str}'
        except Exception:
            return '—'

    @admin.action(description='Продлить триал на срок из настроек биллинга')
    def extend_trial_14_days(self, request, queryset):
        """Продлевает триал через единый биллинговый сервис."""
        from apps.billing.services import BillingService, TRIAL_DAYS

        extended = 0
        errors = []
        for tenant in queryset:
            try:
                BillingService.extend_trial(tenant, days=TRIAL_DAYS)
                extended += 1
            except Exception as exc:
                errors.append(f'{tenant.slug}: {exc}')
        self.message_user(
            request,
            f'Триал продлён для {extended} тенант(ов) на {TRIAL_DAYS} дней.',
        )
        if errors:
            self.message_user(request, '; '.join(errors), level=messages.WARNING)


@admin.register(APIKey)
class APIKeyAdmin(ModelAdmin):
    """Администрирование API-ключей тенантов."""

    list_display = [
        'key_prefix', 'tenant', 'name', 'role', 'is_active',
        'expires_at', 'revoked_at', 'created_at',
    ]
    list_filter = ['tenant', 'role', 'is_active']
    readonly_fields = [
        'tenant', 'key_prefix', 'key_hash', 'role', 'scopes', 'is_active', 'expires_at',
        'created_by', 'revoked_at', 'revoked_by', 'last_used_at',
        'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WebhookEndpoint)
class WebhookEndpointAdmin(ModelAdmin):
    """Администрирование вебхук-эндпоинтов тенантов."""

    list_display = ['tenant', 'url', 'is_active', 'get_events_count', 'created_at']
    list_filter = ['is_active', 'tenant']
    search_fields = ['tenant__name', 'url']
    readonly_fields = ['created_at', 'updated_at']
    fieldsets = [
        ('Основное', {
            'fields': ['tenant', 'url', 'is_active', 'events'],
        }),
        ('Служебное', {
            'fields': ['created_at', 'updated_at'],
            'classes': ['collapse'],
        }),
    ]

    @admin.display(description='Событий')
    def get_events_count(self, obj):
        """Возвращает количество подписанных событий."""
        return len(obj.events) if obj.events else 0


@admin.register(WebhookEvent)
class WebhookEventAdmin(ModelAdmin):
    list_display = ['id', 'tenant', 'event_type', 'created_at']
    list_filter = ['event_type', 'tenant']
    search_fields = ['id', 'idempotency_key']
    readonly_fields = ['id', 'tenant', 'event_type', 'payload', 'idempotency_key', 'created_at', 'updated_at']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(ModelAdmin):
    list_display = ['id', 'event', 'endpoint_url', 'status', 'attempts', 'response_status', 'created_at']
    list_filter = ['status', 'response_status']
    search_fields = ['event__id', 'endpoint_url', 'last_error']
    readonly_fields = [
        'event', 'endpoint', 'endpoint_url', 'status', 'attempts', 'max_attempts',
        'next_attempt_at', 'last_attempt_at', 'delivered_at', 'response_status',
        'response_body', 'last_error', 'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
