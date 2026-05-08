from rest_framework import serializers

from apps.products.models import Product, ProductImage


class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = ['id', 's3_key', 's3_key_thumb', 'url_source', 'position', 'uploaded_at']


class ProductSerializer(serializers.ModelSerializer):
    images = ProductImageSerializer(many=True, read_only=True)

    class Meta:
        model = Product
        fields = [
            'id', 'uuid_1c', 'article', 'name', 'brand', 'category_1c',
            'condition', 'price', 'stock_qty', 'warehouse',
            'export_enabled', 'sync_at', 'images', 'created_at', 'updated_at',
        ]
        read_only_fields = ['uuid_1c', 'sync_at', 'created_at', 'updated_at']
