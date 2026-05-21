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

    def _abs(self, path: str) -> str:
        """Строит абсолютный URL: для S3 уже абсолютный, для FileSystem — через request."""
        url = default_storage.url(path)
        if url.startswith('/'):
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(url)
        return url

    def get_url(self, obj: ProductImage) -> str:
        """Возвращает публичный URL оригинала."""
        return self._abs(obj.s3_key) if obj.s3_key else ''

    def get_thumb_url(self, obj: ProductImage) -> str:
        """Возвращает публичный URL миниатюры."""
        return self._abs(obj.s3_key_thumb) if obj.s3_key_thumb else ''
