from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.tenants.models import APIKey, Tenant, TenantUser

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
        fields = ['id', 'name', 'slug', 'is_active', 'trial_ends_at',
                  'active_listings_count', 'sku_count', 'ai_credits_used',
                  'created_at', 'updated_at']
        read_only_fields = ['id', 'active_listings_count', 'sku_count',
                            'ai_credits_used', 'created_at', 'updated_at']


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
