from rest_framework import serializers

from apps.marketplaces.models import AvitoCategory, CategoryMapping, Listing, MarketplaceAccount


class AvitoCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = AvitoCategory
        fields = ['avito_id', 'name', 'parent_id', 'is_leaf']


class CategoryMappingSerializer(serializers.ModelSerializer):
    class Meta:
        model = CategoryMapping
        fields = ['id', 'marketplace', 'category_source', 'category_target',
                  'category_id', 'attributes_map', 'version', 'created_at']
        read_only_fields = ['version', 'created_at']


class CategoryMappingWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = CategoryMapping
        fields = ['category_source', 'category_target', 'category_id', 'attributes_map']


class MarketplaceAccountSerializer(serializers.ModelSerializer):
    """Чтение: credentials не возвращаются никогда."""

    class Meta:
        model = MarketplaceAccount
        fields = ['id', 'name', 'marketplace', 'external_id', 'is_active', 'created_at']
        read_only_fields = ['created_at']


class ListingSerializer(serializers.ModelSerializer):
    """Листинг для Dashboard — без credentials, с denormalized полями."""

    product_article = serializers.CharField(source='product.article', read_only=True)
    product_name = serializers.CharField(source='product.name', read_only=True)
    account_name = serializers.CharField(source='account.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Listing
        fields = [
            'id', 'status', 'status_display',
            'product_article', 'product_name', 'account_name',
            'title', 'price_on_listing', 'external_id', 'external_url',
            'rejection_reason', 'retry_count', 'published_at', 'created_at',
        ]
        read_only_fields = fields


class MarketplaceAccountWriteSerializer(serializers.Serializer):
    """Запись: принимает client_id/client_secret, шифрует через Fernet."""

    name = serializers.CharField(max_length=200)
    marketplace = serializers.ChoiceField(
        choices=MarketplaceAccount.MARKETPLACE_CHOICES,
        default=MarketplaceAccount.MARKETPLACE_AVITO,
    )
    external_id = serializers.CharField(max_length=100)
    client_id = serializers.CharField(write_only=True)
    client_secret = serializers.CharField(write_only=True)
