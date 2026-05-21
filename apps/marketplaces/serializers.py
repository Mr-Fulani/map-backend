from django.conf import settings
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

    product_id = serializers.IntegerField(source='product.pk', read_only=True)
    product_article = serializers.CharField(source='product.article', read_only=True)
    product_name = serializers.CharField(source='product.name', read_only=True)
    account_name = serializers.CharField(source='account.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Listing
        fields = [
            'id', 'status', 'status_display',
            'product_id', 'product_article', 'product_name', 'account_name',
            'title', 'price_on_listing', 'external_id', 'external_url',
            'rejection_reason', 'retry_count', 'published_at', 'created_at',
        ]
        read_only_fields = fields


def _image_url(s3_key: str, fallback: str, request=None) -> str:
    """Строит URL изображения: CDN (только при S3) → default_storage → fallback."""
    from django.core.files.storage import default_storage
    cdn = getattr(settings, 'YC_CDN_DOMAIN', '')
    # CDN используем только если storage реально S3 (FileSystemStorage не имеет bucket_name)
    is_s3 = hasattr(default_storage, 'bucket_name')
    if cdn and s3_key and is_s3:
        return f'https://{cdn}/{s3_key}'
    if s3_key:
        url = default_storage.url(s3_key)
        if url.startswith('/') and request:
            return request.build_absolute_uri(url)
        return url
    return fallback or ''


class ListingDetailSerializer(ListingSerializer):
    """
    Расширенный сериализатор листинга для дровера предпросмотра.

    Добавляет AI-поля и список изображений товара.
    """

    description_ai = serializers.CharField(read_only=True)
    ai_confidence = serializers.FloatField(read_only=True)
    ai_confidence_display = serializers.SerializerMethodField()
    images = serializers.SerializerMethodField()

    class Meta(ListingSerializer.Meta):
        fields = ListingSerializer.Meta.fields + [
            'description_ai', 'ai_confidence', 'ai_confidence_display', 'images',
        ]
        read_only_fields = fields

    def get_ai_confidence_display(self, obj) -> str:
        """Возвращает уверенность AI в виде строки с процентами."""
        if obj.ai_confidence is None:
            return '—'
        pct = round(obj.ai_confidence * 100)
        if pct >= 70:
            label = 'Высокая'
        elif pct >= 50:
            label = 'Средняя'
        else:
            label = 'Низкая'
        return f'{label} ({pct}%)'

    def get_images(self, obj) -> list:
        """Возвращает список изображений товара с CDN-ссылками."""
        request = self.context.get('request')
        return [
            {
                'id': img.pk,
                'url': _image_url(img.s3_key, img.url_source, request),
                'thumb_url': _image_url(img.s3_key_thumb, img.url_source, request),
                'position': img.position,
                'is_primary': img.is_primary,
            }
            for img in obj.product.images.order_by('position')
        ]


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
