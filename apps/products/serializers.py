from django.core.files.storage import default_storage
from rest_framework import serializers

from apps.products.models import Product, ProductImage


class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = ['id', 's3_key', 's3_key_thumb', 'url_source', 'position', 'uploaded_at']


class ProductSerializer(serializers.ModelSerializer):
    images = ProductImageSerializer(many=True, read_only=True)
    images_count = serializers.SerializerMethodField()
    primary_thumb_url = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id', 'uuid_1c', 'article', 'name', 'brand', 'category_1c',
            'condition', 'price', 'stock_qty', 'warehouse',
            'export_enabled', 'sync_at', 'images', 'images_count', 'primary_thumb_url',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['uuid_1c', 'sync_at', 'created_at', 'updated_at']

    def get_images_count(self, obj) -> int:
        """Возвращает количество изображений товара."""
        return len(obj.images.all())

    def get_primary_thumb_url(self, obj) -> str:
        """Возвращает URL миниатюры главного (или первого) изображения."""
        images = list(obj.images.all())
        img = next((i for i in images if i.is_primary), None) or (images[0] if images else None)
        if not img or not img.s3_key_thumb:
            return ''
        url = default_storage.url(img.s3_key_thumb)
        if url.startswith('/'):
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(url)
        return url
