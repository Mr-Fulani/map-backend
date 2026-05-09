from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.tenants.models import APIKey, Tenant, TenantUser


class TenantUserInline(admin.TabularInline):
    """Инлайн пользователей тенанта в карточке тенанта."""

    model = TenantUser
    extra = 0
    readonly_fields = ['user', 'role', 'created_at']
    can_delete = False


@admin.register(Tenant)
class TenantAdmin(ModelAdmin):
    """
    Администрирование тенантов.

    Показывает имя, план подписки, статус активности и счётчики использования.
    """

    list_display = [
        'name', 'slug', 'is_active', 'get_plan',
        'active_listings_count', 'sku_count', 'ai_credits_used', 'trial_ends_at', 'created_at',
    ]
    list_filter = ['is_active']
    search_fields = ['name', 'slug']
    readonly_fields = [
        'active_listings_count', 'sku_count', 'ai_credits_used',
        'created_at', 'updated_at',
    ]
    inlines = [TenantUserInline]
    fieldsets = [
        ('Основное', {
            'fields': ['name', 'slug', 'is_active'],
        }),
        ('Подписка и триал', {
            'fields': ['trial_ends_at'],
            'description': 'Дата окончания триала устанавливается автоматически при регистрации.',
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

    @admin.display(description='Тариф')
    def get_plan(self, obj):
        """Возвращает название тарифного плана тенанта."""
        try:
            return obj.subscription.plan.name
        except Exception:
            return '—'


@admin.register(APIKey)
class APIKeyAdmin(ModelAdmin):
    """Администрирование API-ключей тенантов."""

    list_display = ['key_prefix', 'tenant', 'name', 'is_active', 'created_at']
    list_filter = ['tenant', 'is_active']
    readonly_fields = ['key_prefix', 'key_hash', 'created_at']
