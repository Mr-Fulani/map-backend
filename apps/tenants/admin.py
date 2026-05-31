from datetime import timedelta

from django import forms
from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html
from unfold.admin import ModelAdmin

from apps.tenants.models import APIKey, CatalogDomain, Tenant, TenantUser, WebhookEndpoint


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


@admin.register(Tenant)
class TenantAdmin(ModelAdmin):
    """
    Администрирование тенантов.

    Показывает имя, план подписки, статус триала и счётчики использования.
    """

    list_display = [
        'name', 'slug', 'catalog_domain', 'is_active', 'get_plan',
        'get_trial_status', 'active_listings_count', 'sku_count', 'created_at',
    ]
    list_filter = ['is_active', 'catalog_domain']
    search_fields = ['name', 'slug']
    readonly_fields = [
        'get_owner_phone', 'get_subscription_info',
        'active_listings_count', 'sku_count', 'ai_credits_used',
        'created_at', 'updated_at',
    ]
    actions = ['extend_trial_14_days']
    inlines = [TenantUserInline]
    fieldsets = [
        ('Основное', {
            'fields': ['name', 'slug', 'catalog_domain', 'is_active'],
        }),
        ('Владелец', {
            'fields': ['get_owner_phone'],
            'description': 'Email владельца — в инлайне пользователей ниже.',
        }),
        ('Подписка', {
            'fields': ['get_subscription_info'],
            'description': 'Управление подпиской — в разделе Биллинг → Подписки.',
        }),
        ('Счётчики (кэш)', {
            'fields': ['active_listings_count', 'sku_count', 'ai_credits_used'],
            'classes': ['collapse'],
            'description': 'Обновляются фоновой задачей. Не редактировать вручную.',
        }),
        ('Служебное', {
            'fields': ['created_at', 'updated_at'],
            'classes': ['collapse'],
        }),
    ]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        choices = [
            (domain.slug, domain.name)
            for domain in CatalogDomain.objects.filter(is_active=True).order_by('sort_order', 'name')
        ]
        if choices:
            form.base_fields['catalog_domain'] = forms.ChoiceField(
                choices=choices,
                label='Домен каталога',
                help_text='Список доменов управляется суперюзером в разделе “Домены каталога”.',
            )
        return form

    @admin.display(description='Тариф')
    def get_plan(self, obj):
        """Возвращает название тарифного плана тенанта."""
        try:
            return obj.subscription.plan.name
        except Exception:
            return '—'

    @admin.display(description='Триал')
    def get_trial_status(self, obj):
        """
        Возвращает цветной индикатор статуса триала.

        Берёт дату из Subscription.current_period_end (актуальна всегда).
        Зелёный — > 3 дней, оранжевый — ≤ 3 дней, красный — истёк.
        """
        end_date = None
        try:
            sub = obj.subscription
            if sub.status in ('trial', 'active', 'past_due'):
                end_date = sub.current_period_end
        except Exception:
            pass

        if not end_date:
            return '—'

        # current_period_end — DateField, сравниваем с date()
        today = timezone.now().date()
        days = (end_date - today).days

        if days < 0:
            return format_html(
                '<span style="color:#ef4444;font-weight:600">Истёк</span>'
            )
        if days <= 3:
            return format_html(
                '<span style="color:#f97316;font-weight:600">{} дн.</span>', days + 1
            )
        return format_html(
            '<span style="color:#22c55e;font-weight:600">{} дн.</span>', days + 1
        )

    @admin.display(description='Телефон владельца')
    def get_owner_phone(self, obj):
        """Возвращает телефон владельца тенанта (роль owner)."""
        membership = obj.members.filter(role='owner').select_related('user').first()
        if membership:
            return membership.user.phone or '—'
        return '—'

    @admin.display(description='Подписка')
    def get_subscription_info(self, obj):
        """Показывает статус и срок подписки одной строкой."""
        try:
            sub = obj.subscription
            end = sub.current_period_end
            end_str = end.strftime('%d.%m.%Y') if end else '∞'
            return f'{sub.plan.name} / {sub.get_status_display()} / до {end_str}'
        except Exception:
            return '—'

    @admin.action(description='Продлить триал на 14 дней')
    def extend_trial_14_days(self, request, queryset):
        """Продлевает триал выбранных тенантов на 14 дней и синхронизирует подписку."""
        today = timezone.now().date()
        extended = 0
        for tenant in queryset:
            try:
                sub = tenant.subscription
                base = sub.current_period_end if sub.current_period_end and sub.current_period_end > today else today
                new_end = base + timedelta(days=14)
                sub.current_period_end = new_end
                sub.status = 'trial'
                sub.save(update_fields=['current_period_end', 'status'])
                # trial_ends_at — DateTimeField, конвертируем date → datetime
                tenant.trial_ends_at = timezone.make_aware(
                    timezone.datetime.combine(new_end, timezone.datetime.min.time())
                )
                tenant.save(update_fields=['trial_ends_at'])
                extended += 1
            except Exception:
                pass
        self.message_user(request, f'Триал продлён для {extended} тенант(ов) на 14 дней.')


@admin.register(APIKey)
class APIKeyAdmin(ModelAdmin):
    """Администрирование API-ключей тенантов."""

    list_display = ['key_prefix', 'tenant', 'name', 'is_active', 'created_at']
    list_filter = ['tenant', 'is_active']
    readonly_fields = ['key_prefix', 'key_hash', 'created_at']


@admin.register(WebhookEndpoint)
class WebhookEndpointAdmin(ModelAdmin):
    """Администрирование вебхук-эндпоинтов тенантов."""

    list_display = ['tenant', 'url', 'is_active', 'get_events_count', 'created_at']
    list_filter = ['is_active', 'tenant']
    search_fields = ['tenant__name', 'url']
    readonly_fields = ['secret', 'created_at', 'updated_at']
    fieldsets = [
        ('Основное', {
            'fields': ['tenant', 'url', 'is_active', 'events'],
        }),
        ('Безопасность', {
            'fields': ['secret'],
            'description': 'HMAC-секрет для подписи запросов. Генерируется при создании.',
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
