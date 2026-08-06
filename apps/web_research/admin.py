from django import forms
from django.contrib import admin, messages
from django.db import transaction
from unfold.admin import ModelAdmin, TabularInline

from apps.core.admin import TenantScopedReadOnlyAdminMixin
from apps.tenants.models import Tenant
from apps.web_research.models import (
    CompetitorOffer, TenantWebResearchSettings, WebResearchClaim,
    WebResearchEvidence, WebResearchRun, WebSearchAttempt, WebSearchConnection,
)
from apps.web_research.providers.registry import (
    create_search_provider, registered_search_providers,
)


class ResearchTenantFilter(admin.SimpleListFilter):
    title = 'Тенант'
    parameter_name = 'tenant'

    def lookups(self, request, model_admin):
        tenants = Tenant.objects.filter(web_research_runs__isnull=False)
        if not request.user.is_superuser:
            tenants = tenants.filter(members__user=request.user)
        return [
            (str(tenant_id), name)
            for tenant_id, name in tenants.order_by('name').distinct().values_list('id', 'name')
        ]

    def queryset(self, request, queryset):
        if not self.value():
            return queryset
        lookup = (
            'tenant_id'
            if queryset.model is WebResearchRun
            else 'run__tenant_id'
        )
        return queryset.filter(**{lookup: self.value()})


class EvidenceInline(TabularInline):
    model = WebResearchEvidence
    extra = 0
    fields = ['rank', 'provider_id', 'domain', 'title', 'url', 'query']
    readonly_fields = fields
    can_delete = False


class AttemptInline(TabularInline):
    model = WebSearchAttempt
    extra = 0
    fields = [
        'provider_id', 'status', 'query', 'result_count', 'duration_ms',
        'error_message', 'created_at',
    ]
    readonly_fields = fields
    can_delete = False


class ClaimInline(TabularInline):
    model = WebResearchClaim
    extra = 0
    fields = [
        'claim_type', 'confidence', 'review_status', 'payload',
        'saved_model', 'saved_record_id',
    ]
    readonly_fields = fields
    can_delete = False


@admin.register(WebResearchRun)
class WebResearchRunAdmin(TenantScopedReadOnlyAdminMixin, ModelAdmin):
    list_display = [
        'id', 'product', 'tenant', 'status', 'purpose', 'trigger', 'search_provider',
        'result_count', 'claim_count', 'offer_count', 'created_at',
    ]
    list_filter = [
        ResearchTenantFilter, 'status', 'purpose', 'trigger', 'search_provider',
        'ai_provider', 'created_at',
    ]
    search_fields = [
        'product__article', 'product__name', 'tenant__name', 'tenant__slug', 'queries',
    ]
    list_select_related = ['tenant', 'product']
    date_hierarchy = 'created_at'
    readonly_fields = [
        'tenant', 'product', 'status', 'trigger', 'purpose', 'settings_snapshot', 'search_provider',
        'ai_provider', 'ai_model', 'queries', 'coverage_before', 'coverage_after',
        'result_count', 'claim_count', 'offer_count', 'generate_after', 'error_message',
        'started_at', 'finished_at', 'created_at', 'updated_at',
    ]
    inlines = [AttemptInline, EvidenceInline, ClaimInline]


@admin.register(WebResearchEvidence)
class WebResearchEvidenceAdmin(TenantScopedReadOnlyAdminMixin, ModelAdmin):
    tenant_lookup = 'run__tenant_id'
    list_display = ['id', 'get_tenant', 'run', 'domain', 'rank', 'title', 'created_at']
    list_filter = [ResearchTenantFilter, 'domain', 'created_at']
    search_fields = [
        'url', 'title', 'query', 'run__product__article',
        'run__tenant__name', 'run__tenant__slug',
    ]
    list_select_related = ['run__tenant', 'run__product']
    date_hierarchy = 'created_at'
    readonly_fields = [
        'run', 'query', 'rank', 'provider_id', 'title', 'url', 'domain', 'snippet', 'raw_content',
        'created_at', 'updated_at',
    ]

    @admin.display(description='Тенант', ordering='run__tenant__name')
    def get_tenant(self, obj):
        return obj.run.tenant


class WebSearchConnectionForm(forms.ModelForm):
    ROUTING_ROLE_CHOICES = [
        ('primary', 'Основной'),
        ('fallback', 'Резервный'),
    ]

    api_key = forms.CharField(
        required=False,
        label='API-ключ',
        help_text='Оставьте пустым, чтобы сохранить текущий ключ или ключ сервера.',
        widget=forms.PasswordInput(render_value=False),
    )
    allowed_plan_slugs = forms.MultipleChoiceField(
        required=False,
        label='Доступно тарифам',
        help_text='Не выбирайте тарифы, чтобы разрешить подключение всем.',
        widget=forms.CheckboxSelectMultiple,
    )
    routing_role = forms.ChoiceField(
        choices=ROUTING_ROLE_CHOICES,
        label='Роль в поиске',
        help_text=(
            'Основной сервис вызывается первым. Резервный используется, если основной '
            'недоступен, исчерпал лимит или не вернул результатов.'
        ),
        widget=forms.RadioSelect,
    )

    class Meta:
        model = WebSearchConnection
        fields = [
            'provider_id', 'display_name', 'is_active', 'routing_role',
            'allowed_plan_slugs', 'parameters', 'requests_per_minute',
            'monthly_request_limit',
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [
            (provider_id, provider.display_name or provider_id)
            for provider_id, provider in registered_search_providers().items()
        ]
        self.fields['provider_id'].widget = forms.Select(choices=choices)
        from apps.billing.models import Plan
        self.fields['allowed_plan_slugs'].choices = list(
            Plan.objects.filter(is_active=True).values_list('slug', 'name')
        )
        if self.instance and self.instance.pk:
            self.initial['allowed_plan_slugs'] = self.instance.allowed_plan_slugs
            self.initial['routing_role'] = (
                'primary'
                if self.instance.priority == WebSearchConnection.PRIMARY_PRIORITY
                else 'fallback'
            )
        elif not WebSearchConnection.objects.filter(
            priority=WebSearchConnection.PRIMARY_PRIORITY,
        ).exists():
            self.initial['routing_role'] = 'primary'
        else:
            self.initial['routing_role'] = 'fallback'

    def clean_provider_id(self):
        provider_id = self.cleaned_data['provider_id']
        if provider_id not in registered_search_providers():
            raise forms.ValidationError('Для этого провайдера нет установленного адаптера.')
        return provider_id

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.priority = (
            WebSearchConnection.PRIMARY_PRIORITY
            if self.cleaned_data['routing_role'] == 'primary'
            else WebSearchConnection.FALLBACK_PRIORITY
        )
        if self.cleaned_data.get('api_key'):
            instance.set_credentials({'api_key': self.cleaned_data['api_key']})
        if commit:
            instance.save()
        return instance


@admin.register(WebSearchConnection)
class WebSearchConnectionAdmin(ModelAdmin):
    form = WebSearchConnectionForm
    list_display = [
        'display_name', 'provider_id', 'routing_role_display', 'is_active',
        'credential_state', 'allowed_plans', 'monthly_usage',
        'last_check_status', 'last_checked_at',
    ]
    list_filter = ['is_active', 'provider_id', 'last_check_status']
    actions = ['make_primary', 'make_fallback', 'check_connections']
    readonly_fields = [
        'credential_state', 'last_check_status', 'last_check_message',
        'last_checked_at', 'created_at', 'updated_at',
    ]

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            if obj.priority == WebSearchConnection.PRIMARY_PRIORITY:
                WebSearchConnection.objects.exclude(pk=obj.pk).filter(
                    priority=WebSearchConnection.PRIMARY_PRIORITY,
                ).update(priority=WebSearchConnection.FALLBACK_PRIORITY)
            super().save_model(request, obj, form, change)

    @admin.display(description='Роль', ordering='priority')
    def routing_role_display(self, obj):
        if obj.priority == WebSearchConnection.PRIMARY_PRIORITY:
            return 'Основной'
        return 'Резервный'

    @admin.action(description='Назначить выбранный сервис основным')
    def make_primary(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(
                request,
                'Выберите ровно один сервис, который станет основным.',
                level=messages.WARNING,
            )
            return
        connection = queryset.get()
        with transaction.atomic():
            WebSearchConnection.objects.exclude(pk=connection.pk).filter(
                priority=WebSearchConnection.PRIMARY_PRIORITY,
            ).update(priority=WebSearchConnection.FALLBACK_PRIORITY)
            connection.priority = WebSearchConnection.PRIMARY_PRIORITY
            connection.save(update_fields=['priority', 'updated_at'])
        self.message_user(request, f'{connection.display_name} назначен основным.')

    @admin.action(description='Назначить выбранные сервисы резервными')
    def make_fallback(self, request, queryset):
        updated = queryset.update(priority=WebSearchConnection.FALLBACK_PRIORITY)
        self.message_user(request, f'Резервных сервисов: {updated}.')

    @admin.display(description='Ключ')
    def credential_state(self, obj):
        if obj and obj.has_credentials:
            return 'Сохранён зашифрованно'
        provider = create_search_provider(obj.provider_id) if obj else None
        return 'Настроен на сервере' if provider and provider.is_available() else 'Не задан'

    @admin.display(description='Тарифы')
    def allowed_plans(self, obj):
        return ', '.join(obj.allowed_plan_slugs) if obj.allowed_plan_slugs else 'Все'

    @admin.display(description='Запросов за месяц')
    def monthly_usage(self, obj):
        from django.utils.timezone import now
        current = now()
        used = obj.attempts.filter(
            created_at__year=current.year, created_at__month=current.month,
        ).count()
        return f'{used} / {obj.monthly_request_limit or "∞"}'

    @admin.action(description='Проверить выбранные подключения')
    def check_connections(self, request, queryset):
        ok_count = 0
        for connection in queryset:
            provider = create_search_provider(
                connection.provider_id,
                credentials=connection.get_credentials(),
                parameters=connection.parameters,
            )
            if provider is None or not provider.is_available():
                connection.mark_checked(ok=False, message='API-ключ не настроен.')
                continue
            try:
                provider.search('Kia Optima 92402D4000 автозапчасть', count=1)
            except Exception as exc:
                connection.mark_checked(ok=False, message=str(exc))
            else:
                connection.mark_checked(ok=True, message='Подключение работает.')
                ok_count += 1
        self.message_user(
            request,
            f'Работают подключений: {ok_count} из {queryset.count()}.',
            level=messages.SUCCESS if ok_count else messages.WARNING,
        )


@admin.register(WebSearchAttempt)
class WebSearchAttemptAdmin(TenantScopedReadOnlyAdminMixin, ModelAdmin):
    tenant_lookup = 'run__tenant_id'
    list_display = [
        'id', 'provider_id', 'status', 'run', 'result_count',
        'duration_ms', 'created_at',
    ]
    list_filter = ['provider_id', 'status', 'created_at']
    search_fields = ['query', 'error_message', 'run__product__article']
    readonly_fields = [
        'run', 'connection', 'provider_id', 'query', 'status', 'result_count',
        'duration_ms', 'retryable', 'error_code', 'error_message',
        'created_at', 'updated_at',
    ]


@admin.register(WebResearchClaim)
class WebResearchClaimAdmin(TenantScopedReadOnlyAdminMixin, ModelAdmin):
    tenant_lookup = 'run__tenant_id'
    list_display = [
        'id', 'get_tenant', 'run', 'claim_type', 'confidence',
        'review_status', 'saved_model', 'saved_record_id',
    ]
    list_filter = [ResearchTenantFilter, 'claim_type', 'review_status', 'created_at']
    search_fields = [
        'run__product__article', 'run__product__name',
        'run__tenant__name', 'run__tenant__slug',
    ]
    list_select_related = ['run__tenant', 'run__product']
    date_hierarchy = 'created_at'
    readonly_fields = [
        'run', 'claim_type', 'payload', 'confidence', 'review_status', 'evidence',
        'saved_model', 'saved_record_id', 'created_at', 'updated_at',
    ]

    @admin.display(description='Тенант', ordering='run__tenant__name')
    def get_tenant(self, obj):
        return obj.run.tenant


@admin.register(CompetitorOffer)
class CompetitorOfferAdmin(TenantScopedReadOnlyAdminMixin, ModelAdmin):
    tenant_lookup = 'tenant_id'
    list_display = [
        'id', 'tenant', 'product', 'seller_name', 'domain', 'price', 'currency',
        'availability', 'match_type', 'review_status', 'captured_at', 'expires_at',
    ]
    list_filter = [
        'tenant', 'provider_id', 'country_code', 'availability', 'condition',
        'match_type', 'review_status', 'captured_at',
    ]
    search_fields = [
        'product__article', 'product__name', 'seller_name', 'domain', 'url',
        'article', 'matched_code', 'tenant__name', 'tenant__slug',
    ]
    list_select_related = ['tenant', 'product', 'run', 'evidence']
    date_hierarchy = 'captured_at'
    readonly_fields = [field.name for field in CompetitorOffer._meta.fields]


@admin.register(TenantWebResearchSettings)
class TenantWebResearchSettingsAdmin(ModelAdmin):
    list_display = [
        'tenant', 'market_research_enabled', 'region_preset', 'search_language',
        'result_limit', 'price_ttl_hours', 'updated_at',
    ]
    list_filter = ['market_research_enabled', 'region_preset', 'include_used', 'include_analogues']
    search_fields = ['tenant__name', 'tenant__slug']
    list_select_related = ['tenant']
