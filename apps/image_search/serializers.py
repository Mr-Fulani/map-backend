"""Сериализаторы для API управления изображениями товаров."""

from django.core.files.storage import default_storage
from rest_framework import serializers

from apps.products.models import ProductImage


class ProductImageSerializer(serializers.ModelSerializer):
    """Полное представление изображения товара для API.

    Вместо s3_key возвращает публичные URL через CDN.
    """

    url = serializers.SerializerMethodField()
    thumb_url = serializers.SerializerMethodField()

    class Meta:
        model = ProductImage
        fields = [
            'id', 'status', 'source_id', 'tier', 'quality_score',
            'search_confidence', 'is_primary', 'position',
            'resolution_w', 'resolution_h', 'file_size_kb',
            'url', 'thumb_url', 'url_source', 'uploaded_at',
        ]

    def get_url(self, obj: ProductImage) -> str:
        """Возвращает публичный URL оригинала через CDN."""
        return default_storage.url(obj.s3_key) if obj.s3_key else ''

    def get_thumb_url(self, obj: ProductImage) -> str:
        """Возвращает публичный URL миниатюры через CDN."""
        return default_storage.url(obj.s3_key_thumb) if obj.s3_key_thumb else ''
