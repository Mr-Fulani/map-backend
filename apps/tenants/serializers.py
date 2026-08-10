from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import serializers

from apps.tenants.models import (
    APIKey, API_KEY_SCOPE_CHOICES, API_KEY_SCOPES, API_KEY_WRITE_SCOPES,
    CatalogDomain, Tenant, TenantUser, WEBHOOK_EVENTS,
    WebhookDelivery, WebhookEndpoint,
)

User = get_user_model()


class RegisterSerializer(serializers.Serializer):
    """Регистрация нового тенанта."""

    name = serializers.CharField(max_length=200)
    slug = serializers.RegexField(
        regex=r'^[a-z0-9-]+$',
        max_length=50,
        error_messages={
            'invalid': 'URL-идентификатор должен содержать только английские буквы, цифры и дефисы.',
        },
    )
    email = serializers.EmailField()
    password = serializers.CharField(max_length=256, write_only=True, trim_whitespace=False)

    def validate_slug(self, value):
        if Tenant.objects.filter(slug=value).exists():
            raise serializers.ValidationError('Этот slug уже занят.')
        return value

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError(
                'Пользователь с таким email уже существует. Войдите в аккаунт.',
            )
        return value

    def validate(self, attrs):
        candidate = User(email=attrs.get('email', ''))
        try:
            validate_password(attrs.get('password', ''), user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)}) from exc
        return attrs


class TenantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tenant
        fields = ['id', 'name', 'slug', 'is_active', 'catalog_domain', 'trial_ends_at',
                  'active_listings_count', 'sku_count', 'ai_credits_used',
                  'ai_credit_limit_override',
                  'created_at', 'updated_at']
        read_only_fields = ['id', 'active_listings_count', 'sku_count',
                            'ai_credits_used', 'ai_credit_limit_override',
                            'created_at', 'updated_at']


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
        fields = [
            'id', 'name', 'key_prefix', 'role', 'scopes', 'is_active',
            'expires_at', 'revoked_at', 'last_used_at', 'created_at',
        ]
        read_only_fields = fields


class APIKeyCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    role = serializers.ChoiceField(
        choices=APIKey.ROLE_CHOICES,
        default=APIKey.ROLE_VIEWER,
    )
    scopes = serializers.ListField(
        child=serializers.ChoiceField(choices=API_KEY_SCOPE_CHOICES),
        required=False,
    )
    expires_in_days = serializers.IntegerField(
        min_value=1,
        max_value=365,
        default=90,
    )

    def validate(self, attrs):
        scopes = attrs.get('scopes')
        if scopes is None:
            from apps.tenants.models import default_api_key_scopes
            scopes = default_api_key_scopes()
        if len(scopes) != len(set(scopes)):
            raise serializers.ValidationError({'scopes': 'Scopes must be unique.'})
        if set(scopes) - API_KEY_SCOPES:
            raise serializers.ValidationError({'scopes': 'Unknown scope.'})
        if (
            attrs['role'] == APIKey.ROLE_VIEWER
            and API_KEY_WRITE_SCOPES.intersection(scopes)
        ):
            raise serializers.ValidationError({
                'scopes': 'Viewer API key cannot receive write scopes.',
            })
        attrs['scopes'] = sorted(scopes)
        attrs['expires_at'] = timezone.now() + timedelta(
            days=attrs.pop('expires_in_days')
        )
        return attrs


class APIKeyCreatedSerializer(APIKeySerializer):
    """API key response shown once immediately after creation."""

    key = serializers.CharField(read_only=True)
    warning = serializers.CharField(read_only=True)

    class Meta(APIKeySerializer.Meta):
        fields = [*APIKeySerializer.Meta.fields, 'key', 'warning']


class WebhookEndpointSerializer(serializers.ModelSerializer):
    """Сериализатор вебхук-эндпоинта для чтения."""

    class Meta:
        model = WebhookEndpoint
        fields = ['id', 'url', 'events', 'is_active', 'created_at']
        read_only_fields = ['id', 'is_active', 'created_at']


class WebhookEndpointWriteSerializer(serializers.Serializer):
    """Сериализатор создания вебхук-эндпоинта."""

    url = serializers.URLField(max_length=500)
    events = serializers.ListField(
        child=serializers.ChoiceField(choices=WEBHOOK_EVENTS),
        min_length=1,
        max_length=len(WEBHOOK_EVENTS),
    )

    def validate_url(self, value):
        from urllib.parse import urlsplit

        from apps.core.url_security import (
            UnsafePublicURL,
            resolve_public_http_transport_url,
        )

        if urlsplit(value).scheme.lower() != 'https':
            raise serializers.ValidationError(
                'Webhook URL должен использовать HTTPS.',
            )
        try:
            target = resolve_public_http_transport_url(value)
        except UnsafePublicURL as exc:
            raise serializers.ValidationError(
                'Webhook URL должен вести на публичный HTTPS-адрес.',
            ) from exc
        # Store one canonical representation so default ports, host case and
        # fragments cannot bypass the per-tenant duplicate constraint.
        return target.url


class WebhookEndpointCreatedSerializer(WebhookEndpointSerializer):
    """Webhook response containing its one-time plaintext signing secret."""

    secret = serializers.CharField(read_only=True)
    warning = serializers.CharField(read_only=True)

    class Meta(WebhookEndpointSerializer.Meta):
        fields = [*WebhookEndpointSerializer.Meta.fields, 'secret', 'warning']


class WebhookDeliverySerializer(serializers.ModelSerializer):
    event_id = serializers.UUIDField(source='event.id', read_only=True)
    event_type = serializers.CharField(source='event.event_type', read_only=True)

    class Meta:
        model = WebhookDelivery
        fields = [
            'id', 'event_id', 'event_type', 'endpoint_url', 'status', 'attempts',
            'max_attempts', 'next_attempt_at', 'last_attempt_at', 'delivered_at',
            'response_status', 'last_error', 'created_at',
        ]
