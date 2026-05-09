from datetime import timedelta

from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html
from unfold.admin import ModelAdmin

from apps.tenants.models import APIKey, Tenant, TenantUser


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
        'name', 'slug', 'is_active', 'get_plan',
        'get_trial_status', 'active_listings_count', 'sku_count', 'created_at',
    ]
    list_filter = ['is_active']
    search_fields = ['name', 'slug']
    readonly_fields = [
        'get_owner_phone',
        'active_listings_count', 'sku_count', 'ai_credits_used',
        'created_at', 'updated_at',
    ]
    actions = ['extend_trial_14_days']
    inlines = [TenantUserInline]
    fieldsets = [
        ('Основное', {
            'fields': ['name', 'slug', 'is_active'],
        }),
        ('Владелец', {
            'fields': ['get_owner_phone'],
            'description': 'Email владельца — в инлайне пользователей ниже.',
        }),
        ('Подписка и триал', {
            'fields': ['trial_ends_at'],
            'description': 'Устанавливается автоматически при регистрации. Можно продлить вручную или через экшен.',
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

    @admin.display(description='Триал')
    def get_trial_status(self, obj):
        """
        Возвращает цветной индикатор статуса триала.

        Зелёный — > 3 дней, оранжевый — ≤ 3 дней, красный — истёк.
        """
        if not obj.trial_ends_at:
            return '—'
        now = timezone.now()
        delta = obj.trial_ends_at - now
        days = delta.days
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

    @admin.action(description='Продлить триал на 14 дней')
    def extend_trial_14_days(self, request, queryset):
        """Продлевает триал выбранных тенантов на 14 дней от текущей даты или от окончания триала."""
        now = timezone.now()
        extended = 0
        for tenant in queryset:
            # Продлеваем от текущего окончания или от сегодня (если уже истёк)
            base = tenant.trial_ends_at if tenant.trial_ends_at and tenant.trial_ends_at > now else now
            tenant.trial_ends_at = base + timedelta(days=14)
            tenant.save(update_fields=['trial_ends_at'])
            # Синхронизируем с подпиской
            try:
                sub = tenant.subscription
                if sub.status in ('trial', 'past_due'):
                    sub.current_period_end = tenant.trial_ends_at
                    sub.status = 'trial'
                    sub.save(update_fields=['current_period_end', 'status'])
            except Exception:
                pass
            extended += 1
        self.message_user(request, f'Триал продлён для {extended} тенант(ов) на 14 дней.')


@admin.register(APIKey)
class APIKeyAdmin(ModelAdmin):
    """Администрирование API-ключей тенантов."""

    list_display = ['key_prefix', 'tenant', 'name', 'is_active', 'created_at']
    list_filter = ['tenant', 'is_active']
    readonly_fields = ['key_prefix', 'key_hash', 'created_at']
