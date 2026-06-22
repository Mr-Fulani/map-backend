from django.conf import settings
from rest_framework import serializers

from apps.marketplaces.models import (
    AvitoCategory,
    CategoryMapping,
    Listing,
    MarketplaceAccount,
    MarketplacePlacementAddress,
)
from apps.products.media import get_publishable_product_images


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
        fields = [
            'id', 'name', 'marketplace', 'external_id', 'is_active',
            'default_address', 'default_seller_address_id',
            'default_manager_name', 'default_contact_phone',
            'created_at',
        ]
        read_only_fields = ['created_at']


class MarketplacePlacementAddressSerializer(serializers.ModelSerializer):
    account_name = serializers.CharField(source='account.name', read_only=True)

    class Meta:
        model = MarketplacePlacementAddress
        fields = [
            'id', 'account', 'account_name', 'name', 'seller_address_id', 'address',
            'manager_name', 'contact_phone', 'is_default', 'is_active',
            'created_at',
        ]
        read_only_fields = ['created_at']


class ListingSerializer(serializers.ModelSerializer):
    """Листинг для Dashboard — без credentials, с denormalized полями."""

    product_id = serializers.IntegerField(source='product.pk', read_only=True)
    product_article = serializers.CharField(source='product.article', read_only=True)
    product_name = serializers.CharField(source='product.name', read_only=True)
    account_id = serializers.IntegerField(source='account.pk', read_only=True)
    account_name = serializers.CharField(source='account.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Listing
        fields = [
            'id', 'status', 'status_display',
            'product_id', 'product_article', 'product_name', 'account_id', 'account_name',
            'title', 'price_on_listing', 'external_id', 'external_url',
            'ad_type',
            'placement_address',
            'address_override', 'seller_address_id_override',
            'manager_name_override', 'contact_phone_override',
            'bulk_placement_address',
            'bulk_address', 'bulk_seller_address_id',
            'bulk_manager_name', 'bulk_contact_phone',
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
        images = [
            {
                'id': img.pk,
                'url': _image_url(img.s3_key, img.url_source, request),
                'thumb_url': _image_url(img.s3_key_thumb, img.url_source, request),
                'position': img.position,
                'is_primary': img.is_primary,
            }
            for img in get_publishable_product_images(obj.product)
        ]
        if images:
            return images
        category = getattr(obj.product, 'catalog_category', None)
        if category and category.default_image_s3_key:
            url = _image_url(category.default_image_s3_key, '', request)
            return [{
                'id': None,
                'url': url,
                'thumb_url': url,
                'position': 0,
                'is_primary': True,
                'source': 'category_default',
            }]
        return []


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


class MarketplaceAccountPlacementSerializer(serializers.Serializer):
    default_address = serializers.CharField(max_length=500, required=False, allow_blank=True)
    default_seller_address_id = serializers.CharField(max_length=100, required=False, allow_blank=True)
    default_manager_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    default_contact_phone = serializers.CharField(max_length=50, required=False, allow_blank=True)


class ListingPlacementSerializer(serializers.Serializer):
    placement_address = serializers.IntegerField(required=False, allow_null=True)
    address_override = serializers.CharField(max_length=500, required=False, allow_blank=True)
    seller_address_id_override = serializers.CharField(max_length=100, required=False, allow_blank=True)
    manager_name_override = serializers.CharField(max_length=100, required=False, allow_blank=True)
    contact_phone_override = serializers.CharField(max_length=50, required=False, allow_blank=True)


class ListingFieldsSerializer(serializers.Serializer):
    account_id = serializers.IntegerField(required=False)
    price_on_listing = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=0)
    ad_type = serializers.ChoiceField(choices=Listing.AD_TYPE_CHOICES, required=False)


class ListingBulkPlacementSerializer(ListingPlacementSerializer):
    listing_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, allow_empty=False,
    )
    account_id = serializers.IntegerField(required=False)
    status = serializers.CharField(max_length=20, required=False, allow_blank=True)
    category_source = serializers.CharField(max_length=300, required=False, allow_blank=True)
    catalog_category_id = serializers.IntegerField(required=False)

    def validate(self, attrs):
        has_filter = any(
            attrs.get(field)
            for field in ('listing_ids', 'account_id', 'status', 'category_source', 'catalog_category_id')
        )
        if not has_filter:
            raise serializers.ValidationError('Укажите хотя бы один фильтр для массового обновления.')
        return attrs


class ListingBulkActionSerializer(ListingPlacementSerializer):
    ACTION_PUBLISH = 'publish'
    ACTION_ARCHIVE = 'archive'
    ACTION_DELETE = 'delete'
    ACTION_UPDATE_PLACEMENT = 'update_placement'
    ACTION_CHOICES = (
        ACTION_PUBLISH,
        ACTION_ARCHIVE,
        ACTION_DELETE,
        ACTION_UPDATE_PLACEMENT,
    )

    action = serializers.ChoiceField(choices=ACTION_CHOICES)
    listing_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, allow_empty=False,
    )
    account_id = serializers.IntegerField(required=False)
    status = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate(self, attrs):
        has_filter = any(attrs.get(field) for field in ('listing_ids', 'account_id', 'status'))
        if not has_filter:
            raise serializers.ValidationError('Укажите хотя бы один фильтр для массового действия.')

        placement_fields = (
            'placement_address',
            'address_override',
            'seller_address_id_override',
            'manager_name_override',
            'contact_phone_override',
        )
        if attrs.get('action') == self.ACTION_UPDATE_PLACEMENT:
            if not any(field in attrs for field in placement_fields):
                raise serializers.ValidationError('Укажите поля размещения для массового обновления.')
        return attrs
