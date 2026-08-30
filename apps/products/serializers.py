from decimal import Decimal

from django.core.files.storage import default_storage
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.marketplaces.listing_delivery import listing_delivery_presentation
from apps.products.models import (
    Product, ProductAttribute, ProductBulkActionJob, ProductCatalogClassification,
    ProductCrossCode, ProductEnrichmentFact, ProductImage, ProductParseJob, TenantCatalogCategory,
    TenantCategoryMapping, VehicleFitment,
)
from apps.products.physical_profiles import (
    MAX_PHYSICAL_DECIMAL, VAT_RATES, normalize_vat_rate, physical_profile_presentation,
)


class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = ['id', 's3_key', 's3_key_thumb', 'url_source', 'position', 'uploaded_at']


class ProductCatalogClassificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCatalogClassification
        fields = [
            'domain', 'confidence', 'source', 'reason', 'needs_review',
            'review_status', 'reviewed_at', 'updated_at',
        ]


class TenantCatalogCategorySerializer(serializers.ModelSerializer):
    default_image_url = serializers.SerializerMethodField()
    root_domain_slug = serializers.CharField(source='root_domain.slug', read_only=True)
    root_domain_name = serializers.CharField(source='root_domain.name', read_only=True)
    path = serializers.SerializerMethodField()
    path_label = serializers.SerializerMethodField()
    depth = serializers.SerializerMethodField()
    has_active_children = serializers.SerializerMethodField()
    is_selectable = serializers.SerializerMethodField()
    effective_margin_pct = serializers.SerializerMethodField()
    margin_inherited_from_id = serializers.SerializerMethodField()
    margin_inherited_from_name = serializers.SerializerMethodField()

    class Meta:
        model = TenantCatalogCategory
        fields = [
            'id', 'name', 'normalized_name', 'parent', 'root_domain',
            'root_domain_slug', 'root_domain_name', 'domain', 'aliases', 'external_source',
            'external_id', 'default_image_s3_key', 'default_image_source_name',
            'default_image_url', 'is_active', 'path', 'path_label', 'depth',
            'has_active_children', 'is_selectable',
            'default_margin_pct', 'effective_margin_pct',
            'margin_inherited_from_id', 'margin_inherited_from_name',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'normalized_name', 'default_image_s3_key',
            'default_image_source_name', 'default_image_url',
            'root_domain_slug', 'root_domain_name', 'created_at', 'updated_at',
        ]

    def get_default_image_url(self, obj) -> str:
        if not obj.default_image_s3_key:
            return ''
        url = default_storage.url(obj.default_image_s3_key)
        if url.startswith('/'):
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(url)
        return url

    def get_path(self, obj) -> list[str]:
        """Возвращает путь внутри домена от родителя к выбранному узлу."""
        category_paths = self.context.get('category_paths')
        if category_paths is not None:
            return category_paths.get(obj.pk, [obj.name])

        path: list[str] = []
        node = obj
        seen = set()
        while node is not None and node.pk not in seen:
            seen.add(node.pk)
            path.insert(0, node.name)
            node = node.parent
        return path

    def get_path_label(self, obj) -> str:
        """Возвращает человекочитаемый полный путь категории."""
        path = self.get_path(obj)
        return ' / '.join(path)

    def get_depth(self, obj) -> int:
        """Возвращает глубину узла внутри дерева категорий."""
        return max(len(self.get_path(obj)) - 1, 0)

    def get_has_active_children(self, obj) -> bool:
        """Показывает наличие активных дочерних узлов."""
        category_parent_ids = self.context.get('category_parent_ids')
        if category_parent_ids is not None:
            return obj.pk in category_parent_ids
        return obj.children.filter(is_active=True).exists()

    def get_is_selectable(self, obj) -> bool:
        """Разрешает назначать только активные конечные категории."""
        return obj.is_active and not self.get_has_active_children(obj)

    def _get_margin_source(self, obj):
        """Находит категорию, от которой фактически берётся наценка."""
        margin_sources = self.context.get('category_margin_sources')
        if margin_sources is not None:
            return margin_sources.get(obj.pk)

        node = obj
        seen = set()
        while node is not None and node.pk not in seen:
            seen.add(node.pk)
            if node.default_margin_pct is not None:
                return node
            node = node.parent
        return None

    def get_effective_margin_pct(self, obj) -> str:
        """Возвращает итоговую наценку с учётом наследования."""
        source = self._get_margin_source(obj)
        return str(source.default_margin_pct if source is not None else 0)

    def get_margin_inherited_from_id(self, obj) -> int | None:
        """Возвращает родителя-источник наценки либо null для собственной."""
        source = self._get_margin_source(obj)
        if source is None or source.pk == obj.pk:
            return None
        return source.pk

    def get_margin_inherited_from_name(self, obj) -> str:
        """Возвращает имя родителя, от которого унаследована наценка."""
        source = self._get_margin_source(obj)
        if source is None or source.pk == obj.pk:
            return ''
        return source.name


class TenantCategoryMappingSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    category_domain = serializers.CharField(source='category.domain', read_only=True)

    class Meta:
        model = TenantCategoryMapping
        fields = [
            'id', 'source_category', 'category', 'category_name',
            'category_domain', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'category_name', 'category_domain', 'created_at', 'updated_at']


class ProductEnrichmentSummarySerializer(serializers.Serializer):
    attributes_count = serializers.IntegerField(min_value=0)
    cross_codes_count = serializers.IntegerField(min_value=0)
    fitments_count = serializers.IntegerField(min_value=0)
    latest_parse_status = serializers.CharField(allow_blank=True)
    latest_parse_at = serializers.DateTimeField(allow_null=True)


class ProductPhysicalFactSerializer(serializers.Serializer):
    source_value = serializers.CharField(allow_null=True)
    map_value = serializers.CharField(allow_null=True)
    effective_value = serializers.CharField(allow_null=True)
    effective_source = serializers.ChoiceField(choices=['1c', 'map', 'missing'])
    source_error = serializers.CharField(allow_blank=True)


class ProductPhysicalProfilePresentationSerializer(serializers.Serializer):
    facts = serializers.DictField(child=ProductPhysicalFactSerializer())
    units = serializers.DictField(child=serializers.CharField())
    complete = serializers.BooleanField()
    missing_fields = serializers.ListField(child=serializers.CharField())
    source_updated_at = serializers.DateTimeField(allow_null=True)
    updated_at = serializers.DateTimeField(allow_null=True)


class ProductPhysicalProfileUpdateSerializer(serializers.Serializer):
    """Tenant-editable MAP fallback; source-prefixed fields are never accepted."""

    barcode = serializers.CharField(max_length=64, allow_blank=True, required=False)
    length_mm = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal('0.001'),
        max_value=MAX_PHYSICAL_DECIMAL, allow_null=True, required=False,
    )
    width_mm = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal('0.001'),
        max_value=MAX_PHYSICAL_DECIMAL, allow_null=True, required=False,
    )
    height_mm = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal('0.001'),
        max_value=MAX_PHYSICAL_DECIMAL, allow_null=True, required=False,
    )
    weight_g = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal('0.001'),
        max_value=MAX_PHYSICAL_DECIMAL, allow_null=True, required=False,
    )
    vat_rate = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal('0'),
        max_value=Decimal('100'), allow_null=True, required=False,
    )

    def validate_barcode(self, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise serializers.ValidationError('Штрихкод содержит недопустимые символы.')
        return value.strip()

    def validate_vat_rate(self, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        try:
            normalized = normalize_vat_rate(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        if normalized not in VAT_RATES:
            raise serializers.ValidationError('Выберите поддерживаемую ставку НДС.')
        return normalized

    def validate(self, attrs):
        allowed = {'barcode', 'length_mm', 'width_mm', 'height_mm', 'weight_g', 'vat_rate'}
        unknown = set(self.initial_data) - allowed
        if unknown:
            raise serializers.ValidationError(
                f'Неподдерживаемые поля: {", ".join(sorted(unknown))}',
            )
        if not set(self.initial_data) & allowed:
            raise serializers.ValidationError('Передайте хотя бы одно поле физических данных.')
        return attrs


class ProductSerializer(serializers.ModelSerializer):
    images = ProductImageSerializer(many=True, read_only=True)
    images_count = serializers.SerializerMethodField()
    primary_thumb_url = serializers.SerializerMethodField()
    ai_status = serializers.SerializerMethodField()
    enrichment_status = serializers.SerializerMethodField()
    enrichment_summary = serializers.SerializerMethodField()
    catalog_classification = ProductCatalogClassificationSerializer(
        read_only=True,
        allow_null=True,
        required=False,
    )
    catalog_category = TenantCatalogCategorySerializer(
        read_only=True,
        allow_null=True,
    )
    brand_ref_name = serializers.CharField(source='brand_ref.name', read_only=True)
    listing_status = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id', 'uuid_1c', 'article', 'name', 'brand', 'brand_ref',
            'brand_ref_name', 'brand_resolution_status', 'brand_confidence',
            'brand_source_id', 'brand_needs_review',
            'category_1c', 'catalog_category',
            'condition', 'price', 'stock_qty', 'warehouse',
            'export_enabled', 'sync_excluded', 'sync_at', 'images', 'images_count', 'primary_thumb_url',
            'title_ai', 'description_ai', 'ai_status', 'enrichment_status',
            'enrichment_summary', 'catalog_classification',
            'listing_status',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'uuid_1c', 'brand_ref', 'brand_ref_name', 'brand_resolution_status',
            'brand_confidence', 'brand_source_id', 'brand_needs_review',
            'sync_at', 'created_at', 'updated_at',
        ]

    def get_images_count(self, obj) -> int:
        """Возвращает количество изображений товара."""
        return len(obj.images.all())

    def get_primary_thumb_url(self, obj) -> str:
        """Возвращает URL миниатюры главного (или первого) изображения."""
        images = list(obj.images.all())
        img = next((i for i in images if i.is_primary), None) or (images[0] if images else None)
        if not img or not img.s3_key_thumb:
            category = getattr(obj, 'catalog_category', None)
            if category and category.default_image_s3_key:
                url = default_storage.url(category.default_image_s3_key)
                if url.startswith('/'):
                    request = self.context.get('request')
                    if request:
                        return request.build_absolute_uri(url)
                return url
            return ''
        url = default_storage.url(img.s3_key_thumb)
        if url.startswith('/'):
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(url)
        return url

    def get_ai_status(self, obj) -> str:
        return 'ready' if obj.title_ai and obj.description_ai else 'missing'

    def get_enrichment_status(self, obj) -> str:
        has_enrichment = (
            getattr(obj, 'attributes_count', 0)
            or getattr(obj, 'cross_codes_count', 0)
            or getattr(obj, 'fitments_count', 0)
        )
        if has_enrichment:
            return 'ready'
        latest_jobs = list(getattr(obj, '_prefetched_objects_cache', {}).get('parse_jobs', []))
        if latest_jobs:
            return latest_jobs[0].status
        return 'missing'

    @extend_schema_field(ProductEnrichmentSummarySerializer())
    def get_enrichment_summary(self, obj) -> dict:
        latest_jobs = list(getattr(obj, '_prefetched_objects_cache', {}).get('parse_jobs', []))
        latest_job = latest_jobs[0] if latest_jobs else None
        return {
            'attributes_count': getattr(obj, 'attributes_count', 0),
            'cross_codes_count': getattr(obj, 'cross_codes_count', 0),
            'fitments_count': getattr(obj, 'fitments_count', 0),
            'latest_parse_status': latest_job.status if latest_job else '',
            'latest_parse_at': latest_job.created_at if latest_job else None,
        }

    def get_listing_status(self, obj) -> str | None:
        """Возвращает статус листинга товара (наиболее приоритетный из всех листингов)."""
        listings = list(getattr(obj, '_prefetched_objects_cache', {}).get('listings', []))
        if not listings:
            return None
        priority = [
            'active', 'pending', 'queued', 'requires_review', 'limit_reached',
            'rejected', 'draft', 'archived', 'deleted',
        ]
        for status_val in priority:
            for listing in listings:
                if listing.status == status_val:
                    return status_val
        return listings[0].status


class ProductAttributeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductAttribute
        fields = ['id', 'source_id', 'name', 'raw_name', 'value', 'created_at']


class ProductCrossCodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCrossCode
        fields = [
            'id', 'source_id', 'manufacturer', 'code',
            'normalized_code', 'code_type', 'created_at',
        ]


class VehicleFitmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = VehicleFitment
        fields = [
            'id', 'source_id', 'source_url', 'make', 'model', 'generation', 'date_from',
            'date_to', 'modification', 'engine_code', 'power_hp',
            'raw_text', 'confidence', 'needs_review', 'review_status', 'reviewed_at', 'created_at',
        ]


class ProductEnrichmentFactSerializer(serializers.ModelSerializer):
    source_label = serializers.SerializerMethodField()

    class Meta:
        model = ProductEnrichmentFact
        fields = [
            'id', 'source_id', 'source_label', 'source_url', 'fact_type', 'name', 'value',
            'raw_text', 'confidence', 'needs_review', 'review_status', 'reviewed_at',
            'created_at', 'updated_at',
        ]

    @extend_schema_field(serializers.CharField())
    def get_source_label(self, obj) -> str:
        from apps.products.source_policy import PART_SOURCE_POLICIES
        policy = PART_SOURCE_POLICIES.get(obj.source_id)
        return policy.label if policy else obj.source_id


class ProductSourceOfferSerializer(serializers.Serializer):
    price = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        allow_null=True,
    )
    currency = serializers.CharField()
    price_is_from = serializers.BooleanField()
    availability = serializers.CharField()
    availability_label = serializers.CharField()
    availability_text = serializers.CharField()
    quantity = serializers.IntegerField(allow_null=True)
    checked_at = serializers.DateTimeField(allow_null=True)


class ProductPriceComparisonSerializer(serializers.Serializer):
    direction = serializers.ChoiceField(
        choices=['equal', 'tenant_higher', 'tenant_lower'],
    )
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    percent = serializers.DecimalField(max_digits=7, decimal_places=1)
    tenant_price = serializers.DecimalField(max_digits=14, decimal_places=2)
    source_price = serializers.DecimalField(max_digits=14, decimal_places=2)


class ProductParseJobSerializer(serializers.ModelSerializer):
    source_label = serializers.SerializerMethodField()
    source_offer = serializers.SerializerMethodField()
    price_comparison = serializers.SerializerMethodField()

    class Meta:
        model = ProductParseJob
        fields = [
            'id', 'product_id', 'brand', 'article', 'normalized_article',
            'source_id', 'source_label', 'source_url', 'status', 'error_message',
            'parsed_data', 'duration_ms', 'created_at', 'updated_at',
            'started_at', 'finished_at', 'source_offer', 'price_comparison',
        ]

    @extend_schema_field(serializers.CharField())
    def get_source_label(self, obj) -> str:
        from apps.products.source_policy import PART_SOURCE_POLICIES
        policy = PART_SOURCE_POLICIES.get(obj.source_id)
        return policy.label if policy else obj.source_id

    @extend_schema_field(ProductSourceOfferSerializer())
    def get_source_offer(self, obj) -> dict:
        return {
            'price': str(obj.source_price) if obj.source_price is not None else None,
            'currency': obj.source_currency,
            'price_is_from': obj.source_price_is_from,
            'availability': obj.source_availability,
            'availability_label': obj.get_source_availability_display(),
            'availability_text': obj.source_availability_text,
            'quantity': obj.source_quantity,
            'checked_at': obj.finished_at or obj.updated_at,
        }

    @extend_schema_field(ProductPriceComparisonSerializer(allow_null=True))
    def get_price_comparison(self, obj) -> dict | None:
        if obj.source_price is None or obj.product_id is None:
            return None
        tenant_price = obj.product.price
        source_price = obj.source_price
        if tenant_price <= 0 or source_price <= 0:
            return None
        difference = tenant_price - source_price
        if difference == 0:
            direction = 'equal'
        elif difference > 0:
            direction = 'tenant_higher'
        else:
            direction = 'tenant_lower'
        percent = (abs(difference) / source_price * Decimal('100')).quantize(Decimal('0.1'))
        return {
            'direction': direction,
            'amount': str(abs(difference).quantize(Decimal('0.01'))),
            'percent': str(percent),
            'tenant_price': str(tenant_price),
            'source_price': str(source_price),
        }


class ProductListingOptionSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    status = serializers.CharField()
    status_display = serializers.CharField()
    account_name = serializers.CharField()
    title = serializers.CharField()


class ProductDetailSerializer(ProductSerializer):
    attributes = serializers.SerializerMethodField()
    cross_codes = ProductCrossCodeSerializer(many=True, read_only=True)
    fitments = VehicleFitmentSerializer(many=True, read_only=True)
    enrichment_facts = ProductEnrichmentFactSerializer(many=True, read_only=True)
    latest_parse_job = serializers.SerializerMethodField()
    parse_jobs_summary = serializers.SerializerMethodField()
    listing_options = serializers.SerializerMethodField()
    physical_profile = serializers.SerializerMethodField()

    class Meta(ProductSerializer.Meta):
        fields = ProductSerializer.Meta.fields + [
            'attributes', 'cross_codes', 'fitments', 'enrichment_facts',
            'latest_parse_job', 'parse_jobs_summary', 'listing_options',
            'physical_profile',
        ]

    @extend_schema_field(ProductPhysicalProfilePresentationSerializer())
    def get_physical_profile(self, obj) -> dict:
        return physical_profile_presentation(obj)

    @extend_schema_field(ProductAttributeSerializer(many=True))
    def get_attributes(self, obj) -> list[dict]:
        from apps.products.attribute_presentation import presented_attributes

        result: list[dict] = []
        for item, name, value in presented_attributes(obj.attributes.all()):
            payload = dict(ProductAttributeSerializer(item).data)
            payload['name'] = name
            payload['value'] = value
            result.append(payload)
        return result

    def _jobs_by_priority(self, obj):
        """Последний job на каждый source_id, отсортированный по убыванию приоритета."""
        from apps.products.source_policy import PART_SOURCE_POLICIES
        seen: dict = {}
        for job in obj.parse_jobs.order_by('-created_at')[:30]:
            if job.source_id not in seen:
                seen[job.source_id] = job
        return sorted(
            seen.values(),
            key=lambda j: PART_SOURCE_POLICIES.get(j.source_id, type('_', (), {'priority': 0})()).priority,
            reverse=True,
        )

    @extend_schema_field(ProductParseJobSerializer(allow_null=True))
    def get_latest_parse_job(self, obj) -> dict | None:
        jobs = self._jobs_by_priority(obj)
        return ProductParseJobSerializer(jobs[0]).data if jobs else None

    @extend_schema_field(ProductParseJobSerializer(many=True))
    def get_parse_jobs_summary(self, obj) -> list[dict]:
        return [ProductParseJobSerializer(j).data for j in self._jobs_by_priority(obj)]

    @extend_schema_field(ProductListingOptionSerializer(many=True))
    def get_listing_options(self, obj) -> list[dict]:
        return [
            {
                'id': listing.pk,
                'status': listing.status,
                'status_display': listing_delivery_presentation(listing).label,
                'account_name': listing.account.name,
                'title': listing.title,
            }
            for listing in obj.listings.all()
            if listing.status != 'deleted'
        ]


class ProductBulkActionJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductBulkActionJob
        fields = [
            'id', 'action', 'source_id', 'status', 'total_count',
            'queued_count', 'processed_count', 'success_count',
            'skipped_count', 'failed_count', 'batch_size', 'pause_seconds',
            'next_batch_at', 'error_message', 'created_at', 'updated_at',
            'started_at', 'finished_at',
        ]
