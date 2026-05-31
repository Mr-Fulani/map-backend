from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.tenants.models import (
    APIKey, CatalogDomain, Tenant, TenantUser, WEBHOOK_EVENTS, WebhookEndpoint,
)

User = get_user_model()


class RegisterSerializer(serializers.Serializer):
    """Регистрация нового тенанта."""

    name = serializers.CharField(max_length=200)
    slug = serializers.SlugField(max_length=50)
    email = serializers.EmailField()
    password = serializers.CharField(min_length=8, write_only=True)

    def validate_slug(self, value):
        if Tenant.objects.filter(slug=value).exists():
            raise serializers.ValidationError('Этот slug уже занят.')
        return value

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            # Пользователь уже существует — это допустимо (можно создать новый тенант)
            pass
        return value


class TenantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tenant
        fields = ['id', 'name', 'slug', 'is_active', 'catalog_domain', 'trial_ends_at',
                  'active_listings_count', 'sku_count', 'ai_credits_used',
                  'created_at', 'updated_at']
        read_only_fields = ['id', 'active_listings_count', 'sku_count',
                            'ai_credits_used', 'created_at', 'updated_at']


class CatalogDomainSerializer(serializers.ModelSerializer):
    is_enabled_for_tenant = serializers.SerializerMethodField()

    class Meta:
        model = CatalogDomain
        fields = [
            'id', 'slug', 'name', 'short_name', 'description',
            'seo_title', 'seo_description', 'seo_keywords', 'seo_h1',
            'canonical_path', 'og_title', 'og_description', 'og_image_url',
            'meta_robots', 'is_active', 'is_system', 'sort_order',
            'supports_auto_parts_enrichment', 'requires_product_classification',
            'is_enabled_for_tenant',
        ]

    def get_is_enabled_for_tenant(self, obj) -> bool:
        tenant = self.context.get('tenant')
        if tenant is None:
            return False
        enabled_ids = self.context.get('enabled_domain_ids')
        if enabled_ids is not None:
            return obj.id in enabled_ids
        return obj.tenant_enablings.filter(tenant=tenant, is_enabled=True).exists()


class TenantUserSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source='user.email', read_only=True)

    class Meta:
        model = TenantUser
        fields = ['id', 'email', 'role', 'created_at']
        read_only_fields = ['id', 'created_at']


class APIKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = APIKey
        fields = ['id', 'name', 'key_prefix', 'is_active', 'last_used_at', 'created_at']
        read_only_fields = ['id', 'key_prefix', 'last_used_at', 'created_at']


class APIKeyCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)


class WebhookEndpointSerializer(serializers.ModelSerializer):
    """Сериализатор вебхук-эндпоинта для чтения."""

    class Meta:
        model = WebhookEndpoint
        fields = ['id', 'url', 'secret', 'events', 'is_active', 'created_at']
        read_only_fields = ['id', 'secret', 'is_active', 'created_at']


class WebhookEndpointWriteSerializer(serializers.Serializer):
    """Сериализатор создания вебхук-эндпоинта."""

    url = serializers.URLField(max_length=500)
    events = serializers.ListField(
        child=serializers.ChoiceField(choices=WEBHOOK_EVENTS),
        min_length=1,
        max_length=len(WEBHOOK_EVENTS),
    )
