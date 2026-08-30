import uuid
from datetime import datetime
from typing import Any, TypedDict, cast

from PIL import Image, UnidentifiedImageError
from django.conf import settings
from django.core.files.storage import default_storage
from django.db.models import (
    Case, CharField, Count, F, IntegerField, OuterRef, Prefetch, Q, Subquery,
    Value, When,
)
from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from django.shortcuts import get_object_or_404
from django.utils.timezone import now
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from apps.tenants.api_views import CatalogAPIView as APIView
from apps.tenants.principals import human_user_or_none

from apps.core.pagination import MapPagination
from apps.core.idempotency import (
    IdempotencyConflict,
    canonical_payload_fingerprint,
    raise_on_fingerprint_conflict,
)
from apps.core.image_security import validate_image_pixel_budget
from apps.core.storage import delete_storage_keys, delete_storage_keys_on_commit
from apps.products.enrichment import normalize_part_code
from apps.core.throttling import (
    PrincipalScopedRateThrottle,
    TenantScopedRateThrottle,
    consume_transactional_tenant_daily_budget,
)
from apps.products.api_schema import (
    CatalogCategoryBranchToggleRequestSerializer,
    CatalogCategoryBranchToggleResponseSerializer,
    CatalogCategoryImageRequestSerializer,
    CatalogCategoryListResponseSerializer,
    CatalogCategoryResponseSerializer,
    CategoryMappingListResponseSerializer,
    CategoryMappingResponseSerializer,
    CrossCodeListResponseSerializer,
    DeletedCountResponseSerializer,
    EnrichmentFactListResponseSerializer,
    EnrichmentFactResponseSerializer,
    FitmentListResponseSerializer,
    FitmentResponseSerializer,
    ProductArchiveResponseSerializer,
    ProductBrandOptionsResponseSerializer,
    ProductFeedFieldUpdateRequestSerializer,
    ProductBulkActionRequestSerializer,
    ProductBulkActionResponseSerializer,
    ProductBulkDeleteRequestSerializer,
    ProductCategoryAssignRequestSerializer,
    ProductCategoryAssignResponseSerializer,
    ProductDetailResponseSerializer,
    ProductExcludeRequestSerializer,
    ProductListResponseSerializer,
    ProductMarketplaceTargetsRequestSerializer,
    ProductParseJobResponseSerializer,
    ProductParseRequestSerializer,
    ProductParseResponseSerializer,
    ProductPhysicalProfileResponseSerializer,
    ProductPublishResponseSerializer,
    ProductRegenerateRequestSerializer,
    ProductRegenerateErrorSerializer,
    ProductRegenerateResponseSerializer,
    IdempotencyConflictResponseSerializer,
    ProductResponseSerializer,
    ReviewQueueActionResponseSerializer,
    ReviewQueueResponseSerializer,
    TaskResponseSerializer,
    TenantSourceCategoryListResponseSerializer,
    UpdatedCountResponseSerializer,
)
from apps.products.models import (
    Product, ProductBulkActionJob, ProductCatalogClassification, ProductCrossCode,
    ProductEnrichmentFact,
    ProductBrand, ProductParseIntent, ProductParseJob, ReviewStatus,
    TenantCatalogCategory, TenantCategoryMapping,
    VehicleFitment,
)
from apps.products.physical_profiles import (
    physical_profile_presentation, update_map_physical_profile,
)
from apps.products.serializers import (
    ProductCatalogClassificationSerializer,
    ProductBulkActionJobSerializer, ProductCrossCodeSerializer, ProductEnrichmentFactSerializer,
    ProductDetailSerializer, ProductParseJobSerializer, ProductPhysicalProfileUpdateSerializer,
    ProductSerializer,
    TenantCatalogCategorySerializer, TenantCategoryMappingSerializer,
    VehicleFitmentSerializer,
)
from apps.products.services import (
    AutoPartsEnrichmentDisabled, ProductBrandService, ProductBulkActionService,
    ProductCategorySeedService, ProductEnrichmentService, ProductIsNotAutoPart,
    ProductKnowledgeGraphService,
)
from apps.marketplaces.models import Listing
from apps.products.tasks import import_from_datasource, sync_product_listings_task
from apps.products.source_policy import DEFAULT_PART_SOURCE, get_part_source_config, get_part_source_policies
from apps.products.storage import media_storage_key
from apps.tenants.models import CatalogDomain, TenantCatalogDomain


class ReviewQueueItem(TypedDict):
    id: str
    type: str
    record_id: int
    product: dict[str, Any]
    title: str
    reason: str
    source_id: str
    confidence: Any
    needs_review: bool
    review_status: str
    created_at: datetime
    updated_at: datetime
    payload: Any


class ReviewQueueRef(TypedDict):
    updated_at: datetime
    item_type: str
    record_id: int


def _validate_tenant_catalog_category(request, serializer, instance=None):
    root_domain = serializer.validated_data.get(
        'root_domain',
        getattr(instance, 'root_domain', None),
    )
    parent = serializer.validated_data.get(
        'parent',
        getattr(instance, 'parent', None),
    )
    if root_domain is None and parent is not None:
        root_domain = parent.root_domain
        serializer.validated_data['root_domain'] = root_domain
    if root_domain is None:
        return Response(
            {
                'status': 'error',
                'code': 'validation_error',
                'message': 'Выберите корневую категорию.',
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not TenantCatalogDomain.objects.filter(
        tenant=request.tenant,
        domain=root_domain,
        is_enabled=True,
    ).exists():
        return Response(
            {
                'status': 'error',
                'code': 'validation_error',
                'message': 'Эта корневая категория не включена для tenant-а.',
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    if parent is not None:
        if parent.tenant_id != request.tenant.id:
            return Response(
                {'status': 'error', 'code': 'validation_error', 'message': 'Родительская категория другого tenant-а'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if instance is not None:
            node = parent
            seen = set()
            while node is not None and node.pk not in seen:
                if node.pk == instance.pk:
                    return Response(
                        {
                            'status': 'error',
                            'code': 'validation_error',
                            'message': 'Категория не может быть родителем самой себе или своего предка.',
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                seen.add(node.pk)
                node = node.parent
        if parent.root_domain_id != root_domain.id:
            return Response(
                {
                    'status': 'error',
                    'code': 'validation_error',
                    'message': 'Подкатегория должна быть внутри той же корневой категории.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
    serializer.validated_data['domain'] = root_domain.slug
    return None


@extend_schema(tags=['Products'])
class ProductListView(APIView):
    """
    GET /api/v1/products/ — список товаров тенанта с поиском и фильтрацией.

    Query params:
        search          — поиск по артикулу, названию, бренду
        export_enabled  — true/false, фильтр по флагу выгрузки
        category_1c     — точное совпадение категории из 1С
        page            — номер страницы (default: 1)
        page_size       — размер страницы (default: 50, max: 500)
    """

    api_key_enabled = True

    @extend_schema(
        operation_id='products_list',
        parameters=[
            OpenApiParameter('search', OpenApiTypes.STR, description='Search by article, name or brand.'),
            OpenApiParameter(
                'listing_filter',
                OpenApiTypes.STR,
                enum=[
                    'listed', 'not_listed', 'active', 'pending', 'queued',
                    'requires_review', 'limit_reached', 'rejected', 'draft',
                    'archived', 'deleted',
                ],
            ),
            OpenApiParameter('category_1c', OpenApiTypes.STR),
            OpenApiParameter('catalog_category', OpenApiTypes.INT),
            OpenApiParameter('catalog_domain', OpenApiTypes.STR),
            OpenApiParameter('needs_review', OpenApiTypes.BOOL),
            OpenApiParameter('sync_excluded', OpenApiTypes.BOOL),
            OpenApiParameter(
                'ordering',
                OpenApiTypes.STR,
                enum=[
                    'price', '-price', 'stock_qty', '-stock_qty',
                    'ai_status', '-ai_status', 'listing_status', '-listing_status',
                ],
            ),
            OpenApiParameter('page', OpenApiTypes.INT),
            OpenApiParameter('page_size', OpenApiTypes.INT),
        ],
        responses=ProductListResponseSerializer,
    )
    def get(self, request):
        latest_jobs = ProductParseJob.objects.order_by('-created_at')
        qs = (
            Product.objects
            .filter(tenant=request.tenant)
            .select_related('catalog_category', 'catalog_classification')
            .prefetch_related('images', Prefetch('parse_jobs', queryset=latest_jobs), 'listings')
            .annotate(
                attributes_count=Count('attributes', distinct=True),
                cross_codes_count=Count('cross_codes', distinct=True),
                fitments_count=Count('fitments', distinct=True),
            )
        )

        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(article__icontains=search)
                | Q(name__icontains=search)
                | Q(brand__icontains=search)
            )

        listing_filter = request.query_params.get('listing_filter', '').strip()
        if listing_filter == 'listed':
            qs = qs.filter(listings__isnull=False).distinct()
        elif listing_filter == 'not_listed':
            qs = qs.filter(listings__isnull=True)
        elif listing_filter in (
            'active', 'pending', 'queued', 'requires_review', 'limit_reached',
            'rejected', 'draft', 'archived', 'deleted',
        ):
            qs = qs.filter(listings__status=listing_filter).distinct()

        category = request.query_params.get('category_1c', '').strip()
        if category:
            qs = qs.filter(category_1c=category)

        catalog_category = request.query_params.get('catalog_category', '').strip()
        if catalog_category:
            qs = qs.filter(catalog_category_id=catalog_category)

        if request.query_params.get('needs_review') == 'true':
            qs = qs.filter(
                Q(catalog_classification__needs_review=True)
                | Q(fitments__needs_review=True)
                | Q(enrichment_facts__needs_review=True)
            ).distinct()

        if request.query_params.get('sync_excluded') == 'true':
            qs = qs.filter(sync_excluded=True)

        domain_counts_qs = qs
        domain_counts = {'all': domain_counts_qs.count()}
        active_domain_slugs = CatalogDomain.objects.filter(is_active=True).values_list('slug', flat=True)
        for domain_slug in active_domain_slugs:
            if domain_slug == 'unknown':
                domain_counts[domain_slug] = domain_counts_qs.filter(
                    Q(catalog_classification__domain='unknown') | Q(catalog_classification__isnull=True)
                ).count()
            else:
                domain_counts[domain_slug] = domain_counts_qs.filter(
                    catalog_classification__domain=domain_slug,
                ).count()

        catalog_domain = request.query_params.get('catalog_domain', '').strip()
        if catalog_domain == 'unknown':
            qs = qs.filter(Q(catalog_classification__domain='unknown') | Q(catalog_classification__isnull=True))
        elif catalog_domain:
            qs = qs.filter(catalog_classification__domain=catalog_domain)

        ordering = request.query_params.get('ordering', '').strip()
        if ordering in ('price', '-price', 'stock_qty', '-stock_qty'):
            qs = qs.order_by(ordering, '-sync_at', '-created_at')
        elif ordering in ('ai_status', '-ai_status'):
            ai_order = Case(
                When(title_ai__isnull=False, description_ai__isnull=False, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            )
            qs = qs.annotate(ai_order=ai_order)
            direction = '' if ordering == 'ai_status' else '-'
            qs = qs.order_by(f'{direction}ai_order', '-sync_at', '-created_at')
        elif ordering in ('listing_status', '-listing_status'):
            listing_priority = Subquery(
                Listing.objects.filter(product=OuterRef('pk'))
                .annotate(priority=Case(
                    When(status='active', then=Value(1)),
                    When(status='pending', then=Value(2)),
                    When(status='queued', then=Value(3)),
                    When(status='requires_review', then=Value(4)),
                    When(status='limit_reached', then=Value(5)),
                    When(status='rejected', then=Value(6)),
                    When(status='draft', then=Value(7)),
                    When(status='archived', then=Value(8)),
                    When(status='deleted', then=Value(9)),
                    output_field=IntegerField(),
                ))
                .order_by('priority')
                .values('priority')[:1]
            )
            qs = qs.annotate(listing_priority=listing_priority)
            if ordering == '-listing_status':
                # активные (priority=1) первые, незалистенные (null) последние
                qs = qs.order_by(F('listing_priority').asc(nulls_last=True), '-sync_at', '-created_at')
            else:
                # незалистенные (null) первые, активные последние
                qs = qs.order_by(F('listing_priority').asc(nulls_first=True), '-sync_at', '-created_at')
        else:
            qs = qs.order_by('-sync_at', '-created_at')

        paginator = MapPagination()
        page = paginator.paginate_queryset(qs, request)
        response = paginator.get_paginated_response(
            ProductSerializer(page, many=True, context={'request': request}).data
        )
        response.data['meta']['domain_counts'] = domain_counts
        return response


@extend_schema(tags=['Products'])
class ProductDetailView(APIView):
    """GET /api/v1/products/{pk}/ — карточка товара."""

    api_key_enabled = True
    api_key_scopes = {
        'GET': {'catalog:read'},
        'HEAD': {'catalog:read'},
        'OPTIONS': {'catalog:read'},
        'PATCH': {'catalog:write', 'listings:write'},
    }

    @extend_schema(
        operation_id='products_retrieve',
        responses=ProductDetailResponseSerializer,
    )
    def get(self, request, pk):
        try:
            product = Product.objects.select_related(
                'catalog_category', 'catalog_classification', 'physical_profile',
            ).prefetch_related(
                'images', 'attributes', 'cross_codes', 'fitments', 'enrichment_facts',
                'parse_jobs', 'listings__account', 'listings__feed_run',
            ).get(
                pk=pk, tenant=request.tenant
            )
        except Product.DoesNotExist:
            return Response(
                {'status': 'error', 'code': 'not_found', 'message': 'Товар не найден'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({'status': 'ok', 'data': ProductDetailSerializer(product, context={'request': request}).data})

    @extend_schema(
        request=ProductFeedFieldUpdateRequestSerializer,
        responses=ProductDetailResponseSerializer,
    )
    def patch(self, request, pk):
        """Точечное редактирование полей товара, которые попадают в Avito XML.

        Бренд нужен для публикации на Avito (обязателен для новых запчастей),
        а из 1С/CSV часто приходит пустым — тенант дозаполняет его вручную.
        Ручное значение не затирается последующими импортами (см.
        ProductService.upsert_from_source).
        """
        request_serializer = ProductFeedFieldUpdateRequestSerializer(data=request.data)
        if not request_serializer.is_valid():
            return Response(
                {
                    'status': 'error',
                    'code': 'validation_error',
                    'message': 'Проверьте поля товара.',
                    **request_serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        from apps.products.feed_writers import (
            StaleProductFeedWrite,
            capture_product_feed_generations,
            locked_product_feed_write,
        )

        has_brand = 'brand' in request_serializer.validated_data
        has_oem = 'avito_oem' in request_serializer.validated_data
        brand = str(request_serializer.validated_data.get('brand') or '').strip()
        avito_oem = str(request_serializer.validated_data.get('avito_oem') or '').strip().upper()
        is_machine = getattr(request.user, 'is_api_key', False)
        brand_ref = ProductBrandService.resolve_existing_brand(brand) if has_brand else None
        brand_changed = False
        oem_changed = False
        product = None
        for _attempt in range(3):
            generations = capture_product_feed_generations((pk,))
            generation = generations.get(pk)
            if generation is None:
                return Response(
                    {'status': 'error', 'code': 'not_found', 'message': 'Товар не найден'},
                    status=status.HTTP_404_NOT_FOUND,
                )
            current_row = Product.objects.filter(pk=pk, tenant=request.tenant).first()
            if current_row is None:
                return Response(
                    {'status': 'error', 'code': 'not_found', 'message': 'Товар не найден'},
                    status=status.HTTP_404_NOT_FOUND,
                )
            brand_changed = has_brand and current_row.brand != brand
            current_oem = ProductCrossCode.objects.filter(
                tenant=request.tenant,
                product_id=pk,
                source_id='avito_manual',
                code_type=ProductCrossCode.CodeType.OEM,
            ).values_list('code', flat=True).first() or ''
            oem_changed = has_oem and current_oem != avito_oem
            try:
                with locked_product_feed_write(
                    (generation,),
                    bump_product_ids=(pk,) if brand_changed or oem_changed else (),
                ) as locked:
                    product = locked[pk]
                    update_fields = []
                    if has_brand:
                        product.brand = brand
                        product.brand_ref = brand_ref
                        if not brand:
                            product.brand_resolution_status = Product.BrandResolutionStatus.UNKNOWN
                            product.brand_confidence = 0.0
                            product.brand_source_id = ''
                            product.brand_needs_review = False
                        elif product.brand_ref is not None:
                            product.brand_resolution_status = Product.BrandResolutionStatus.CATALOG
                            product.brand_confidence = product.brand_ref.confidence
                            product.brand_source_id = 'catalog'
                            product.brand_needs_review = product.brand_ref.needs_review
                        elif is_machine:
                            product.brand_resolution_status = Product.BrandResolutionStatus.SOURCE
                            product.brand_confidence = 0.5
                            product.brand_source_id = 'api_key'
                            product.brand_needs_review = True
                        else:
                            product.brand_resolution_status = Product.BrandResolutionStatus.MANUAL
                            product.brand_confidence = 1.0
                            product.brand_source_id = 'manual'
                            product.brand_needs_review = False
                        update_fields.extend([
                            'brand', 'brand_ref', 'brand_resolution_status', 'brand_confidence',
                            'brand_source_id', 'brand_needs_review',
                        ])
                    if has_oem:
                        ProductCrossCode.objects.filter(
                            tenant=request.tenant,
                            product=product,
                            source_id='avito_manual',
                            code_type=ProductCrossCode.CodeType.OEM,
                        ).delete()
                        if avito_oem:
                            ProductCrossCode.objects.create(
                                tenant=request.tenant,
                                product=product,
                                source_id='avito_manual',
                                manufacturer='',
                                code=avito_oem,
                                normalized_code=normalize_part_code(avito_oem),
                                code_type=ProductCrossCode.CodeType.OEM,
                            )
                        ProductEnrichmentService.refresh_product_denormalized_enrichment(product)
                        update_fields.extend(['oem_numbers', 'cross_numbers', 'applicability'])
                    if update_fields:
                        product.save(update_fields=[*update_fields, 'updated_at'])
                break
            except StaleProductFeedWrite:
                product = None
                continue
        if product is None:
            return Response(
                {
                    'status': 'error',
                    'code': 'concurrent_product_update',
                    'message': 'Товар изменился параллельно. Повторите запрос.',
                },
                status=status.HTTP_409_CONFLICT,
            )
        # Ручные Brand/OEM меняют XML и должны обновить активные листинги.
        if brand_changed or oem_changed:
            transaction.on_commit(lambda: sync_product_listings_task.delay(product.pk, 'content'))
        return Response({'status': 'ok', 'data': ProductDetailSerializer(product, context={'request': request}).data})


@extend_schema(tags=['Products'])
class ProductPhysicalProfileView(APIView):
    """Read or patch MAP fallback without mutating 1C or Avito fields."""

    api_key_enabled = True
    api_key_scopes = {
        'GET': {'catalog:read'},
        'HEAD': {'catalog:read'},
        'OPTIONS': {'catalog:read'},
        'PATCH': {'catalog:write'},
    }

    @extend_schema(
        operation_id='product_physical_profile_retrieve',
        responses=ProductPhysicalProfileResponseSerializer,
    )
    def get(self, request, pk):
        try:
            product = Product.objects.select_related('physical_profile').get(
                pk=pk,
                tenant=request.tenant,
            )
        except Product.DoesNotExist:
            return Response(
                {'status': 'error', 'code': 'not_found', 'message': 'Товар не найден'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({'status': 'ok', 'data': physical_profile_presentation(product)})

    @extend_schema(
        operation_id='product_physical_profile_update',
        request=ProductPhysicalProfileUpdateSerializer,
        responses=ProductPhysicalProfileResponseSerializer,
    )
    def patch(self, request, pk):
        serializer = ProductPhysicalProfileUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    'status': 'error',
                    'code': 'validation_error',
                    'message': 'Проверьте физические данные товара.',
                    **serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            try:
                product = Product.objects.select_for_update().get(
                    pk=pk,
                    tenant=request.tenant,
                )
            except Product.DoesNotExist:
                return Response(
                    {'status': 'error', 'code': 'not_found', 'message': 'Товар не найден'},
                    status=status.HTTP_404_NOT_FOUND,
                )
            profile = update_map_physical_profile(product, serializer.validated_data)
        # Reuse the object cached by the helper so presentation performs no
        # cross-tenant or provider lookup.
        product.physical_profile = profile
        return Response({'status': 'ok', 'data': physical_profile_presentation(product)})


@extend_schema(tags=['Products'])
class ProductBrandOptionsView(APIView):
    """Подсказки брендов для товара: сначала по включённой корневой категории."""

    api_key_enabled = True

    @extend_schema(
        parameters=[
            OpenApiParameter('product_id', OpenApiTypes.INT, required=True),
            OpenApiParameter('q', OpenApiTypes.STR, description='Case-insensitive brand search.'),
        ],
        responses=ProductBrandOptionsResponseSerializer,
    )
    def get(self, request):
        product_id = request.query_params.get('product_id')
        query = request.query_params.get('q', '').strip()
        try:
            product = Product.objects.select_related('catalog_category__root_domain').get(
                pk=product_id, tenant=request.tenant,
            )
        except (Product.DoesNotExist, TypeError, ValueError):
            return Response(
                {'status': 'error', 'code': 'not_found', 'message': 'Товар не найден'},
                status=status.HTTP_404_NOT_FOUND,
            )

        category = product.catalog_category
        root_domain = category.root_domain if category and category.is_active else None
        domain_enabled = bool(root_domain and TenantCatalogDomain.objects.filter(
            tenant=request.tenant, domain=root_domain, is_enabled=True,
        ).exists())

        # ProductBrand.domains — единственная существующая в БД связь бренд ↔
        # категория. Она задана для базового справочника и относится к корневому
        # домену, а не к каждой подкатегории.
        brands = ProductBrand.objects.filter(is_active=True)
        if domain_enabled:
            brands = brands.filter(domains=root_domain)
        else:
            brands = brands.none()
        if query:
            brands = brands.filter(name__icontains=query)
        category_options = list(brands.order_by('name').values_list('name', flat=True).distinct()[:30])
        if product.condition == 'new':
            from apps.marketplaces.adapters.avito.brand_catalog import lookup_brand
            category_options = [name for name in category_options if lookup_brand(name)['known']]

        # Справочник Avito плоский: текущий endpoint source_node не содержит
        # привязки значений Brand к подкатегориям. Добавляем только совпадения по
        # введённому тексту, чтобы дать валидные варианты и не отдавать 11k значений.
        avito_options = []
        catalog_loaded = False
        catalog_synced_at = None
        catalog_stale = True
        from apps.marketplaces.adapters.avito.brand_catalog import catalog_status
        avito_catalog_status = catalog_status()
        catalog_loaded = avito_catalog_status['loaded']
        catalog_synced_at = avito_catalog_status['synced_at']
        catalog_stale = avito_catalog_status['stale']
        if query:
            from apps.marketplaces.adapters.avito.brand_catalog import _catalog_by_normalized_name
            catalog = _catalog_by_normalized_name()
            normalized_query = ''.join(char for char in query.lower() if char.isalnum())
            avito_options = [
                name for normalized, name in catalog.items()
                if normalized_query in normalized
            ][:30]

        options = []
        seen = set()
        for name, source in (
            *((name, 'category') for name in category_options),
            *((name, 'avito') for name in avito_options),
            *((product.brand, 'current') for _ in [None] if product.brand),
        ):
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                options.append({'name': name, 'source': source})

        return Response({
            'status': 'ok',
            'data': {
                'options': options,
                'catalog_loaded': catalog_loaded,
                'catalog_synced_at': catalog_synced_at.isoformat() if catalog_synced_at else None,
                'catalog_stale': catalog_stale,
                'category_scope': (
                    root_domain.name
                    if domain_enabled and root_domain is not None
                    else None
                ),
            },
        })


@extend_schema(tags=['Products'])
class ProductSyncView(APIView):
    """POST /api/v1/products/sync/{connection_id}/ — запустить импорт товаров."""

    api_key_enabled = True
    api_key_scopes = {
        'POST': {'sync:run', 'catalog:write', 'listings:write'},
    }

    @extend_schema(request=None, responses=TaskResponseSerializer)
    def post(self, request, connection_id):
        from apps.datasources.models import DataSourceConnection
        from django.shortcuts import get_object_or_404
        conn = get_object_or_404(
            DataSourceConnection,
            pk=connection_id,
            tenant=request.tenant,
            is_active=True,
        )
        task = import_from_datasource.delay(conn.pk)
        return Response({'status': 'ok', 'data': {'task_id': task.id}})


@extend_schema(tags=['Catalog Categories'])
class TenantCatalogCategoryListView(APIView):
    """GET/POST /api/v1/products/catalog-categories/."""

    api_key_enabled = True

    @extend_schema(
        operation_id='catalog_categories_list',
        parameters=[
            OpenApiParameter(
                'assignable',
                OpenApiTypes.BOOL,
                description='Return active categories only.',
            ),
        ],
        responses=CatalogCategoryListResponseSerializer,
    )
    def get(self, request):
        enabled_domain_ids = TenantCatalogDomain.objects.filter(
            tenant=request.tenant,
            is_enabled=True,
        ).values_list('domain_id', flat=True)
        qs = (
            TenantCatalogCategory.objects
            .filter(tenant=request.tenant, root_domain_id__in=enabled_domain_ids)
            .select_related('root_domain', 'parent')
            .order_by('root_domain__sort_order', 'parent__name', 'name')
        )
        if request.query_params.get('assignable') in ('1', 'true', 'True'):
            qs = qs.filter(is_active=True)

        # В автозапчастях официальное дерево Avito — источник истины. Старое
        # компактное дерево оставляем fallback-ом только до первого импорта Avito.
        has_avito_auto_parts = qs.filter(
            root_domain__slug=TenantCatalogCategory.Domain.AUTO_PARTS,
            external_source='avito',
        ).exists()
        if has_avito_auto_parts:
            qs = qs.exclude(
                root_domain__slug=TenantCatalogCategory.Domain.AUTO_PARTS,
                external_source=ProductCategorySeedService.SEED_SOURCE,
            )

        categories = list(qs)
        categories_by_id: dict[int, TenantCatalogCategory] = {
            category.pk: category
            for category in categories
            if category.pk is not None
        }
        category_paths: dict[int, list[str]] = {}
        category_parent_ids = {
            category.parent_id
            for category in categories
            if category.is_active and category.parent_id is not None
        }
        category_margin_sources: dict[int, TenantCatalogCategory] = {}
        for category in categories:
            category_id = category.pk
            if category_id is None:
                continue
            path: list[str] = []
            node: TenantCatalogCategory | None = category
            seen: set[int] = set()
            while node is not None and node.pk is not None and node.pk not in seen:
                seen.add(node.pk)
                path.insert(0, node.name)
                node = (
                    categories_by_id.get(node.parent_id)
                    if node.parent_id is not None
                    else None
                )
            category_paths[category_id] = path

            node = category
            seen = set()
            while node is not None and node.pk is not None and node.pk not in seen:
                seen.add(node.pk)
                if node.default_margin_pct is not None:
                    category_margin_sources[category_id] = node
                    break
                node = (
                    categories_by_id.get(node.parent_id)
                    if node.parent_id is not None
                    else None
                )

        return Response({
            'status': 'ok',
            'data': TenantCatalogCategorySerializer(
                categories,
                many=True,
                context={
                    'request': request,
                    'category_paths': category_paths,
                    'category_parent_ids': category_parent_ids,
                    'category_margin_sources': category_margin_sources,
                },
            ).data,
        })

    @extend_schema(
        request=TenantCatalogCategorySerializer,
        responses={201: CatalogCategoryResponseSerializer},
    )
    def post(self, request):
        serializer = TenantCatalogCategorySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validation_error = _validate_tenant_catalog_category(request, serializer)
        if validation_error is not None:
            return validation_error
        category = serializer.save(tenant=request.tenant)
        return Response(
            {'status': 'ok', 'data': TenantCatalogCategorySerializer(category, context={'request': request}).data},
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=['Catalog Categories'])
class TenantCatalogCategoryDetailView(APIView):
    """GET/PUT/DELETE /api/v1/products/catalog-categories/{id}/."""

    api_key_enabled = True

    @extend_schema(
        operation_id='catalog_categories_retrieve',
        responses=CatalogCategoryResponseSerializer,
    )
    def get(self, request, pk):
        category = get_object_or_404(TenantCatalogCategory, pk=pk, tenant=request.tenant)
        serializer = TenantCatalogCategorySerializer(category, context={'request': request})
        return Response({'status': 'ok', 'data': serializer.data})

    @extend_schema(
        request=TenantCatalogCategorySerializer,
        responses=CatalogCategoryResponseSerializer,
    )
    def put(self, request, pk):
        from apps.products.feed_writers import (
            StaleProductFeedWrite,
            locked_catalog_category_feed_write,
        )

        category = get_object_or_404(TenantCatalogCategory, pk=pk, tenant=request.tenant)
        serializer = TenantCatalogCategorySerializer(category, data=request.data)
        serializer.is_valid(raise_exception=True)
        validation_error = _validate_tenant_catalog_category(
            request, serializer, instance=category,
        )
        if validation_error is not None:
            return validation_error
        try:
            with locked_catalog_category_feed_write(
                category.pk,
                # The category fence captures its own later exact generation.
                # Treat every accepted update as projection-capable so a
                # concurrent opposite update cannot invalidate a stale
                # pre-lock ``changes_feed`` decision.
                include_descendants=True,
                bump=True,
            ) as (locked_category, _products):
                serializer.instance = locked_category
                category = serializer.save(tenant=request.tenant)
        except StaleProductFeedWrite:
            return Response(
                {
                    'status': 'error',
                    'code': 'concurrent_category_update',
                    'message': 'Категория изменилась параллельно. Повторите запрос.',
                },
                status=status.HTTP_409_CONFLICT,
            )
        serializer = TenantCatalogCategorySerializer(category, context={'request': request})
        return Response({'status': 'ok', 'data': serializer.data})

    @extend_schema(
        request=TenantCatalogCategorySerializer,
        responses=CatalogCategoryResponseSerializer,
    )
    def patch(self, request, pk):
        from apps.products.feed_writers import (
            StaleProductFeedWrite,
            locked_catalog_category_feed_write,
        )

        category = get_object_or_404(TenantCatalogCategory, pk=pk, tenant=request.tenant)
        serializer = TenantCatalogCategorySerializer(category, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        validation_error = _validate_tenant_catalog_category(
            request, serializer, instance=category,
        )
        if validation_error is not None:
            return validation_error
        try:
            with locked_catalog_category_feed_write(
                category.pk,
                include_descendants=True,
                bump=True,
            ) as (locked_category, _products):
                serializer.instance = locked_category
                category = serializer.save()
        except StaleProductFeedWrite:
            return Response(
                {
                    'status': 'error',
                    'code': 'concurrent_category_update',
                    'message': 'Категория изменилась параллельно. Повторите запрос.',
                },
                status=status.HTTP_409_CONFLICT,
            )
        serializer = TenantCatalogCategorySerializer(category, context={'request': request})
        return Response({'status': 'ok', 'data': serializer.data})

    @extend_schema(
        parameters=[
            OpenApiParameter(
                'hard',
                OpenApiTypes.BOOL,
                description='Physically delete an unused category instead of disabling it.',
            ),
        ],
        request=None,
        responses={204: None},
    )
    def delete(self, request, pk):
        category = get_object_or_404(TenantCatalogCategory, pk=pk, tenant=request.tenant)
        # ?hard=true — полное удаление; иначе мягкое отключение (is_active=False).
        if request.query_params.get('hard') in ('1', 'true', 'True'):
            if category.products.exists():
                return Response(
                    {'status': 'error',
                     'message': 'К категории привязаны товары — сначала переназначьте их в другую категорию.'},
                    status=status.HTTP_409_CONFLICT,
                )
            if category.children.exists():
                return Response(
                    {'status': 'error',
                     'message': 'У категории есть подкатегории — сначала удалите или перенесите их.'},
                    status=status.HTTP_409_CONFLICT,
                )
            category.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        category.is_active = False
        category.save(update_fields=['is_active', 'updated_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Catalog Categories'])
class TenantCatalogCategoryBranchToggleView(APIView):
    """POST /api/v1/products/catalog-categories/{id}/toggle-branch/ — вкл/выкл ветку категорий.

    Меняет is_active у категории и всего её поддерева: авто-классификация
    рассматривает только активные категории, поэтому отключение ветки
    («Для грузовиков и спецтехники», «Автомобиль на запчасти» и т.п.)
    повышает точность присвоения подкатегорий. При отключении товары ветки
    ставятся в очередь на переклассификацию.
    """

    api_key_enabled = True

    @extend_schema(
        request=CatalogCategoryBranchToggleRequestSerializer,
        responses=CatalogCategoryBranchToggleResponseSerializer,
    )
    def post(self, request, pk):
        category = get_object_or_404(TenantCatalogCategory, pk=pk, tenant=request.tenant)
        is_active = request.data.get('is_active')
        if not isinstance(is_active, bool):
            return Response(
                {'status': 'error', 'code': 'validation_error', 'message': 'Передайте is_active: true/false'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        children_by_parent: dict[int, list[int]] = {}
        for cat_id, parent_id in TenantCatalogCategory.objects.filter(
            tenant=request.tenant,
        ).values_list('id', 'parent_id'):
            if parent_id is not None:
                children_by_parent.setdefault(parent_id, []).append(cat_id)

        branch_ids = [category.pk]
        queue = [category.pk]
        while queue:
            node_id = queue.pop()
            for child_id in children_by_parent.get(node_id, []):
                branch_ids.append(child_id)
                queue.append(child_id)

        affected_products = 0
        with transaction.atomic():
            affected_categories = TenantCatalogCategory.objects.filter(
                tenant=request.tenant, id__in=branch_ids,
            ).update(is_active=is_active, updated_at=now())
            if not is_active:
                affected_products = Product.objects.filter(
                    tenant=request.tenant, catalog_category_id__in=branch_ids,
                ).count()
                if affected_products:
                    from apps.products.tasks import reclassify_products_for_categories
                    tenant_id, ids = request.tenant.pk, list(branch_ids)
                    transaction.on_commit(
                        lambda: reclassify_products_for_categories.delay(tenant_id, ids)
                    )

        return Response({
            'status': 'ok',
            'data': {
                'is_active': is_active,
                'affected_categories': affected_categories,
                'affected_products': affected_products,
            },
        })


@extend_schema(tags=['Catalog Categories'])
class TenantCatalogCategoryDefaultImageView(APIView):
    """POST /api/v1/products/catalog-categories/{id}/default-image/ — загрузить fallback-картинку."""

    api_key_enabled = True
    api_key_scopes = {
        'POST': {'catalog:write', 'media:write'},
        'DELETE': {'catalog:write', 'media:write'},
    }
    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = [PrincipalScopedRateThrottle, TenantScopedRateThrottle]
    principal_throttle_scope = 'image_upload_principal'
    tenant_throttle_scope = 'image_upload_tenant'

    @extend_schema(
        request=CatalogCategoryImageRequestSerializer,
        responses=CatalogCategoryResponseSerializer,
    )
    def post(self, request, pk):
        from apps.products.feed_writers import (
            StaleProductFeedWrite,
            locked_catalog_category_feed_write,
        )

        category = get_object_or_404(TenantCatalogCategory, pk=pk, tenant=request.tenant)
        image = request.FILES.get('image')
        if image is None:
            return Response(
                {'status': 'error', 'code': 'validation_error', 'message': 'Передайте файл image'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if image.size > settings.MAX_IMAGE_UPLOAD_BYTES:
            return Response(
                {
                    'status': 'error',
                    'code': 'validation_error',
                    'message': (
                        'Размер файла превышает допустимый лимит '
                        f'{settings.MAX_IMAGE_UPLOAD_BYTES} байт.'
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        supported_formats = {
            'JPEG': ('jpg', 'image/jpeg'),
            'PNG': ('png', 'image/png'),
            'WEBP': ('webp', 'image/webp'),
        }
        try:
            decoded = Image.open(image)
            validate_image_pixel_budget(decoded)
            image_format = str(decoded.format or '').upper()
            if image_format not in supported_formats:
                raise ValueError('unsupported image format')
            decoded.verify()
            image.seek(0)
        except (UnidentifiedImageError, OSError, ValueError):
            return Response(
                {
                    'status': 'error',
                    'code': 'validation_error',
                    'message': 'Можно загрузить только корректный JPEG, PNG или WebP.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        extension, canonical_content_type = supported_formats[image_format]
        image.content_type = canonical_content_type
        s3_key = media_storage_key(
            f'catalog-categories/{request.tenant.slug}/{category.pk}/'
            f'{uuid.uuid4().hex}.{extension}',
        )
        saved_key = default_storage.save(s3_key, image)
        try:
            with locked_catalog_category_feed_write(
                category.pk,
                include_descendants=False,
                bump=True,
            ) as (category, _products):
                previous_key = category.default_image_s3_key
                category.default_image_s3_key = saved_key
                category.default_image_source_name = image.name[:255]
                category.save(update_fields=[
                    'default_image_s3_key', 'default_image_source_name', 'updated_at',
                ])
                delete_storage_keys_on_commit((previous_key,))
        except StaleProductFeedWrite:
            delete_storage_keys((saved_key,))
            return Response(
                {
                    'status': 'error',
                    'code': 'concurrent_category_update',
                    'message': 'Категория изменилась параллельно. Повторите запрос.',
                },
                status=status.HTTP_409_CONFLICT,
            )
        except Exception:
            delete_storage_keys((saved_key,))
            raise
        serializer = TenantCatalogCategorySerializer(category, context={'request': request})
        return Response({'status': 'ok', 'data': serializer.data})

    @extend_schema(request=None, responses=CatalogCategoryResponseSerializer)
    def delete(self, request, pk):
        from apps.products.feed_writers import (
            StaleProductFeedWrite,
            locked_catalog_category_feed_write,
        )

        category = get_object_or_404(
            TenantCatalogCategory,
            pk=pk,
            tenant=request.tenant,
        )
        try:
            with locked_catalog_category_feed_write(
                category.pk,
                include_descendants=False,
                bump=True,
            ) as (category, _products):
                previous_key = category.default_image_s3_key
                category.default_image_s3_key = ''
                category.default_image_source_name = ''
                category.save(update_fields=[
                    'default_image_s3_key', 'default_image_source_name', 'updated_at',
                ])
                delete_storage_keys_on_commit((previous_key,))
        except StaleProductFeedWrite:
            return Response(
                {
                    'status': 'error',
                    'code': 'concurrent_category_update',
                    'message': 'Категория изменилась параллельно. Повторите запрос.',
                },
                status=status.HTTP_409_CONFLICT,
            )
        serializer = TenantCatalogCategorySerializer(category, context={'request': request})
        return Response({'status': 'ok', 'data': serializer.data})


@extend_schema(tags=['Catalog Categories'])
class TenantCategoryMappingListView(APIView):
    """GET/POST /api/v1/products/catalog-category-mappings/."""

    api_key_enabled = True

    @extend_schema(responses=CategoryMappingListResponseSerializer)
    def get(self, request):
        qs = (
            TenantCategoryMapping.objects
            .filter(tenant=request.tenant)
            .select_related('category')
            .order_by('source_category')
        )
        return Response({'status': 'ok', 'data': TenantCategoryMappingSerializer(qs, many=True).data})

    @extend_schema(
        request=TenantCategoryMappingSerializer,
        responses={201: CategoryMappingResponseSerializer},
    )
    def post(self, request):
        serializer = TenantCategoryMappingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        category = get_object_or_404(
            TenantCatalogCategory,
            pk=serializer.validated_data['category'].pk,
            tenant=request.tenant,
            root_domain__tenant_enablings__tenant=request.tenant,
            root_domain__tenant_enablings__is_enabled=True,
        )
        mapping, _ = TenantCategoryMapping.objects.update_or_create(
            tenant=request.tenant,
            source_category=serializer.validated_data['source_category'],
            defaults={'category': category},
        )
        return Response(
            {'status': 'ok', 'data': TenantCategoryMappingSerializer(mapping).data},
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=['Catalog Categories'])
class TenantCategoryMappingDetailView(APIView):
    """DELETE /api/v1/products/catalog-category-mappings/{id}/."""

    api_key_enabled = True

    @extend_schema(request=None, responses={204: None})
    def delete(self, request, pk):
        mapping = get_object_or_404(TenantCategoryMapping, pk=pk, tenant=request.tenant)
        mapping.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Catalog Categories'])
class TenantSourceCategoryListView(APIView):
    """GET /api/v1/products/catalog-source-categories/."""

    api_key_enabled = True

    @extend_schema(responses=TenantSourceCategoryListResponseSerializer)
    def get(self, request):
        mappings = {
            mapping.source_category: mapping.category_id
            for mapping in TenantCategoryMapping.objects.filter(tenant=request.tenant)
        }
        categories = [
            {
                'source_category': source_category,
                'catalog_category': mappings.get(source_category),
            }
            for source_category in (
                Product.objects
                .filter(tenant=request.tenant)
                .exclude(category_1c='')
                .order_by('category_1c')
                .values_list('category_1c', flat=True)
                .distinct()
            )
        ]
        return Response({'status': 'ok', 'data': categories})


@extend_schema(tags=['Catalog Categories'])
class ProductCatalogCategoryAssignView(APIView):
    """POST /api/v1/products/catalog-categories/assign/ — назначить категорию товарам."""

    api_key_enabled = True

    @extend_schema(
        request=ProductCategoryAssignRequestSerializer,
        responses=ProductCategoryAssignResponseSerializer,
    )
    def post(self, request):
        serializer = ProductCategoryAssignRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product_ids = serializer.validated_data['product_ids']
        category_id = serializer.validated_data.get('catalog_category')

        category = None
        if category_id:
            enabled_domain_ids = TenantCatalogDomain.objects.filter(
                tenant=request.tenant,
                is_enabled=True,
            ).values_list('domain_id', flat=True)
            category = get_object_or_404(
                TenantCatalogCategory,
                pk=category_id,
                tenant=request.tenant,
                is_active=True,
                root_domain_id__in=enabled_domain_ids,
            )
            if category.children.filter(is_active=True).exists():
                return Response(
                    {
                        'status': 'error',
                        'code': 'category_not_selectable',
                        'message': 'Выберите конечную подкатегорию, а не раздел каталога.',
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        from apps.products.feed_writers import (
            StaleProductFeedWrite,
            capture_product_feed_generations,
            locked_product_feed_write,
        )

        valid_ids = []
        write_completed = False
        for _attempt in range(3):
            valid_ids = list(
                Product.objects
                .filter(tenant=request.tenant, pk__in=product_ids)
                .order_by('pk')
                .values_list('pk', flat=True)
            )
            generations = capture_product_feed_generations(valid_ids)
            if category is None:
                changed_ids = list(
                    Product.objects.filter(pk__in=valid_ids)
                    .filter(catalog_category__isnull=False)
                    .values_list('pk', flat=True)
                )
            else:
                changed_ids = list(
                    Product.objects.filter(pk__in=valid_ids)
                    .exclude(catalog_category_id=category.pk)
                    .values_list('pk', flat=True)
                )
            try:
                with locked_product_feed_write(
                    generations.values(),
                    bump_product_ids=changed_ids,
                ):
                    Product.objects.filter(tenant=request.tenant, pk__in=valid_ids).update(
                        catalog_category=category,
                        catalog_category_manually_cleared=category is None,
                        updated_at=now(),
                    )
                    products = (
                        Product.objects
                        .filter(tenant=request.tenant, pk__in=valid_ids)
                        .select_related('tenant', 'catalog_category')
                    )
                    if category is not None:
                        # Обучение на ручном исправлении: запоминаем «категория
                        # источника → категория каталога», чтобы следующий импорт
                        # с той же категорией 1С не гадал по названию товара.
                        source_categories = set(
                            products.exclude(category_1c='').values_list('category_1c', flat=True)
                        )
                        for source_category in source_categories:
                            TenantCategoryMapping.objects.update_or_create(
                                tenant=request.tenant,
                                source_category=source_category,
                                defaults={'category': category},
                            )
                    for product in products:
                        classification = ProductEnrichmentService._classify_product_catalog_domain_locked(
                            product, force=True,
                        )
                        if category is not None:
                            is_machine = getattr(request.user, 'is_api_key', False)
                            classification.domain = category.domain
                            classification.confidence = 0.95
                            classification.source = (
                                ProductCatalogClassification.Source.API_KEY
                                if is_machine
                                else ProductCatalogClassification.Source.MANUAL
                            )
                            source_label = 'через API Key' if is_machine else 'вручную'
                            classification.reason = (
                                f'Категория каталога выбрана {source_label}: {category.name}.'
                            )
                            classification.needs_review = False
                            classification.review_status = ReviewStatus.APPROVED
                            classification.reviewed_at = now()
                            classification.reviewed_by = _review_actor(request)
                            classification.save(update_fields=[
                                'domain', 'confidence', 'source', 'reason', 'needs_review',
                                'review_status', 'reviewed_at', 'reviewed_by', 'updated_at',
                            ])
                        else:
                            # После снятия ручной категории снова показываем актуальный
                            # результат автоматической классификации без старой отметки оператора.
                            classification.reviewed_at = None
                            classification.reviewed_by = None
                            classification.save(update_fields=[
                                'reviewed_at', 'reviewed_by', 'updated_at',
                            ])
                write_completed = True
                break
            except StaleProductFeedWrite:
                continue

        if not write_completed:
            return Response(
                {
                    'status': 'error',
                    'code': 'concurrent_product_update',
                    'message': 'Товары изменились параллельно. Повторите запрос.',
                },
                status=status.HTTP_409_CONFLICT,
            )

        skipped_count = max(len(set(product_ids)) - len(valid_ids), 0)

        return Response({
            'status': 'ok',
            'data': {
                'updated_count': len(valid_ids),
                'skipped_count': skipped_count,
                'catalog_category': TenantCatalogCategorySerializer(category).data if category else None,
            },
        })


@extend_schema(tags=['Products'])
class ProductExcludeView(APIView):
    """POST /api/v1/products/exclude/ — исключить или восстановить товары из синхронизации."""

    api_key_enabled = True

    @extend_schema(
        request=ProductExcludeRequestSerializer,
        responses=UpdatedCountResponseSerializer,
    )
    def post(self, request):
        """Исключает товары из синхронизации с источником (1С/CSV)."""
        serializer = ProductExcludeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product_ids = serializer.validated_data['product_ids']
        exclude = serializer.validated_data['exclude']
        updated = Product.objects.filter(
            tenant=request.tenant, pk__in=product_ids,
        ).update(sync_excluded=bool(exclude))
        return Response({'status': 'ok', 'data': {'updated_count': updated}})


@extend_schema(tags=['Products'])
class ProductBulkDeleteView(APIView):
    """DELETE /api/v1/products/bulk-delete/ — мягкое удаление товаров."""

    api_key_enabled = True
    api_key_scopes = {
        'DELETE': {'catalog:write', 'listings:write'},
    }

    @extend_schema(
        request=ProductBulkDeleteRequestSerializer,
        responses=DeletedCountResponseSerializer,
    )
    def delete(self, request):
        """Скрывает товары и листинги; retention-задача удалит их физически позднее."""
        from apps.products.feed_writers import (
            StaleProductFeedWrite,
            capture_product_feed_generations,
            locked_product_feed_write,
        )

        serializer = ProductBulkDeleteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product_ids = serializer.validated_data['product_ids']
        deleted_count = 0
        write_completed = False
        for _attempt in range(3):
            valid_ids = list(
                Product.objects.filter(
                    tenant=request.tenant,
                    pk__in=product_ids,
                ).order_by('pk').values_list('pk', flat=True)
            )
            generations = capture_product_feed_generations(valid_ids)
            try:
                with locked_product_feed_write(generations.values()):
                    # The account intent already exists at this point. Hiding
                    # the last ACTIVE/PENDING listing cannot erase the STOP
                    # signal because the desired-state cursor commits with it.
                    Listing.objects.filter(
                        tenant=request.tenant,
                        product_id__in=valid_ids,
                    ).delete()
                    deleted_count, _ = Product.objects.filter(
                        tenant=request.tenant,
                        pk__in=valid_ids,
                    ).delete()
                write_completed = True
                break
            except StaleProductFeedWrite:
                continue
        if not write_completed:
            return Response(
                {
                    'status': 'error',
                    'code': 'concurrent_product_update',
                    'message': 'Товары изменились параллельно. Повторите запрос.',
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response({'status': 'ok', 'data': {'deleted_count': deleted_count}})


@extend_schema(tags=['Products'])
class ProductSearchView(APIView):
    """GET /api/v1/products/search/?brand=&article= — поиск товара tenant-а."""

    api_key_enabled = True

    @extend_schema(
        parameters=[
            OpenApiParameter('brand', OpenApiTypes.STR, required=True),
            OpenApiParameter('article', OpenApiTypes.STR, required=True),
        ],
        responses=ProductResponseSerializer,
    )
    def get(self, request):
        brand = request.query_params.get('brand', '').strip()
        article = request.query_params.get('article', '').strip()
        if not brand or not article:
            return Response(
                {'status': 'error', 'code': 'validation_error',
                 'errors': {'brand': ['Обязательное поле'], 'article': ['Обязательное поле']}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        product = Product.objects.filter(
            tenant=request.tenant,
            brand__iexact=brand,
            article__iexact=article,
        ).select_related('catalog_classification').prefetch_related('images').first()
        if product is None:
            return Response(
                {'status': 'error', 'code': 'not_found', 'message': 'Товар не найден'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({'status': 'ok', 'data': ProductSerializer(product, context={'request': request}).data})


def _idempotency_incomplete_response() -> Response:
    return Response(
        {
            'status': 'error',
            'code': 'idempotency_incomplete',
            'message': 'Исходный результат запроса недоступен.',
        },
        status=status.HTTP_409_CONFLICT,
    )


@extend_schema(tags=['Products'])
class ProductParseView(APIView):
    """POST /api/v1/products/parse/ — поставить enrichment job в очередь."""

    api_key_enabled = True
    throttle_classes = [PrincipalScopedRateThrottle, TenantScopedRateThrottle]
    principal_throttle_scope = 'expensive_research_principal'
    tenant_throttle_scope = 'expensive_research_tenant'
    expensive_throttle_methods = {'POST'}
    api_key_scopes = {'POST': {
        'catalog:write', 'listings:write', 'ai:run',
        'research:run', 'media:write',
    }}

    @extend_schema(
        request=ProductParseRequestSerializer,
        responses={
            201: ProductParseResponseSerializer,
            409: IdempotencyConflictResponseSerializer,
        },
    )
    def post(self, request):
        request_serializer = ProductParseRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        request_data = request_serializer.validated_data
        idempotency_key = request_data['idempotency_key']
        product_id = request_data.get('product_id')
        brand = str(request_data.get('brand') or '').strip()
        article = str(request_data.get('article') or '').strip()
        explicit_source = str(request_data.get('source') or '').strip()
        generate_after = request_data['generate_after']

        raw_intent = {
            'article': article,
            'brand': brand,
            'generate_after': generate_after,
            'product_id': product_id,
            'source': explicit_source,
        }
        request_fingerprint = canonical_payload_fingerprint(raw_intent)
        existing_intent = ProductParseIntent.objects.select_related(
            'primary_job',
        ).filter(
            tenant=request.tenant,
            idempotency_key=idempotency_key,
        ).first()
        if existing_intent is not None:
            try:
                raise_on_fingerprint_conflict(
                    existing_intent.request_fingerprint,
                    request_fingerprint,
                )
            except IdempotencyConflict as exc:
                return Response(
                    {
                        'status': 'error',
                        'code': 'idempotency_conflict',
                        'message': str(exc),
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            primary_job = existing_intent.primary_job
            if primary_job is None:
                return _idempotency_incomplete_response()
            actual_job_ids = list(existing_intent.jobs.order_by('pk').values_list(
                'pk',
                flat=True,
            ))
            if sorted(actual_job_ids) != sorted(existing_intent.job_ids):
                return _idempotency_incomplete_response()
            return Response({
                'status': 'ok',
                'data': {
                    'job_id': primary_job.pk,
                    'job_ids': list(existing_intent.job_ids),
                    'state': primary_job.status,
                    'generate_after': existing_intent.generate_after,
                },
            }, status=status.HTTP_201_CREATED)

        if explicit_source:
            try:
                get_part_source_config(explicit_source)
            except ValueError as exc:
                return Response(
                    {'status': 'error', 'code': 'validation_error', 'message': str(exc)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            sources = [explicit_source]
        else:
            sources = [
                sid for sid, policy in get_part_source_policies().items()
                if policy.capabilities.supports_search
            ]

        if product_id:
            product = get_object_or_404(Product, pk=product_id, tenant=request.tenant)
            brand = brand or product.brand
            article = article or product.article
        else:
            if not brand or not article:
                return Response(
                    {'status': 'error', 'code': 'validation_error',
                     'errors': {'brand': ['Обязательное поле'], 'article': ['Обязательное поле']}},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            qs = Product.objects.filter(
                tenant=request.tenant,
                brand__iexact=brand,
                article__iexact=article,
            )
            count = qs.count()
            if count != 1:
                return Response(
                    {'status': 'error', 'code': 'product_not_found_or_ambiguous',
                     'message': f'Найдено товаров: {count}. Передайте product_id.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            product = qs.get()

        resolved_payload = {
            'article': article,
            'brand': brand,
            'generate_after': generate_after,
            'product_id': product.pk,
            'sources': sorted(sources),
        }
        try:
            # Classification is useful domain state even when this request is
            # rejected as a non-auto-part. Persist it in autocommit before the
            # idempotency transaction so a deliberate 400 response does not
            # roll the classification back together with a not-created intent.
            ProductEnrichmentService.ensure_product_auto_parts_eligible(
                request.tenant,
                product,
            )
            with transaction.atomic():
                type(request.tenant).objects.select_for_update().only('pk').get(
                    pk=request.tenant.pk,
                )
                intent, created = ProductParseIntent.objects.get_or_create(
                    tenant=request.tenant,
                    idempotency_key=idempotency_key,
                    defaults={
                        'request_fingerprint': request_fingerprint,
                        'request_payload': {
                            'intent': raw_intent,
                            'resolved': resolved_payload,
                        },
                        'product': product,
                        'generate_after': generate_after,
                    },
                )
                if created:
                    jobs = [
                        ProductEnrichmentService.create_parse_job(
                            tenant=request.tenant,
                            product=product,
                            brand=brand,
                            article=article,
                            normalized_article=normalize_part_code(article),
                            source_id=source,
                            fallback_origin_key=(
                                f'product-parse-intent:{intent.pk}'
                            ),
                        )
                        for source in sources
                    ]
                    policies = get_part_source_policies()
                    primary_job = max(
                        jobs,
                        key=lambda job: policies[job.source_id].priority,
                    )
                    from apps.core.dispatch import enqueue_durable_task
                    for job in jobs:
                        is_primary = job.pk == primary_job.pk
                        task_name = (
                            'apps.products.tasks.parse_single_part_then_generate_description'
                            if (generate_after and is_primary)
                            else 'apps.products.tasks.parse_single_part'
                        )
                        enqueue_durable_task(
                            task_name,
                            args=[job.pk],
                            deduplication_key=f'product-parse-job:{job.pk}',
                            max_run_attempts=4,
                        )
                    intent.primary_job = primary_job
                    intent.job_ids = [job.pk for job in jobs]
                    intent.save(update_fields=['primary_job', 'job_ids', 'updated_at'])
                    ProductParseJob.objects.filter(
                        pk__in=intent.job_ids,
                    ).update(ingress_intent=intent)
                    # Charge only the first canonical intent. The cost is the
                    # exact number of external parse jobs created; a rejected
                    # budget rolls all DB jobs/dispatch rows back together.
                    consume_transactional_tenant_daily_budget(
                        tenant=request.tenant,
                        scope='product-parse-jobs',
                        cost=len(jobs),
                        limit=settings.PRODUCT_PARSE_TENANT_DAILY_JOBS,
                    )
                else:
                    raise_on_fingerprint_conflict(
                        intent.request_fingerprint,
                        request_fingerprint,
                    )
                    if intent.primary_job_id is None:
                        return _idempotency_incomplete_response()
                    primary_job = ProductParseJob.objects.get(
                        pk=intent.primary_job_id,
                        tenant=request.tenant,
                    )
                    actual_job_ids = list(intent.jobs.order_by('pk').values_list(
                        'pk',
                        flat=True,
                    ))
                    if sorted(actual_job_ids) != sorted(intent.job_ids):
                        return _idempotency_incomplete_response()
        except IdempotencyConflict as exc:
            return Response(
                {
                    'status': 'error',
                    'code': 'idempotency_conflict',
                    'message': str(exc),
                },
                status=status.HTTP_409_CONFLICT,
            )
        except AutoPartsEnrichmentDisabled as exc:
            return Response(
                {'status': 'error', 'code': 'auto_parts_enrichment_disabled', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ProductIsNotAutoPart as exc:
            return Response(
                {'status': 'error', 'code': 'product_is_not_auto_part', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({
            'status': 'ok',
            'data': {
                'job_id': primary_job.pk,
                'job_ids': list(intent.job_ids),
                'state': primary_job.status,
                'generate_after': generate_after,
            },
        }, status=status.HTTP_201_CREATED)


@extend_schema(tags=['Products'])
class ProductParseJobDetailView(APIView):
    """GET /api/v1/products/parse-jobs/{id}/ — статус enrichment job."""

    api_key_enabled = True

    @extend_schema(responses=ProductParseJobResponseSerializer)
    def get(self, request, pk: int):
        job = get_object_or_404(ProductParseJob, pk=pk, tenant=request.tenant)
        return Response({'status': 'ok', 'data': ProductParseJobSerializer(job).data})


@extend_schema(tags=['Products'])
class ProductBulkActionView(APIView):
    """POST /api/v1/products/bulk-actions/ — throttled массовое действие."""

    # Actions span catalog, AI and media domains. Keep machine access denied
    # until authorization can be selected from the validated action payload.
    api_key_scopes = {}
    throttle_classes = [PrincipalScopedRateThrottle, TenantScopedRateThrottle]
    # A single request can fan out paid parsing, AI, or image-provider work.
    # Use the stricter research ceiling for every bulk action; choosing a looser
    # scope from unvalidated request data would make the throttle bypassable.
    principal_throttle_scope = 'expensive_research_principal'
    tenant_throttle_scope = 'expensive_research_tenant'
    expensive_throttle_methods = {'POST'}

    @extend_schema(
        request=ProductBulkActionRequestSerializer,
        responses={
            201: ProductBulkActionResponseSerializer,
            409: IdempotencyConflictResponseSerializer,
        },
    )
    def post(self, request):
        request_serializer = ProductBulkActionRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        request_data = request_serializer.validated_data
        action = request_data['action']
        product_ids = sorted(set(request_data['product_ids']))
        source = str(request_data.get('source') or DEFAULT_PART_SOURCE).strip()
        batch_size = request_data['batch_size']
        pause_seconds = request_data['pause_seconds']
        idempotency_key = request_data['idempotency_key']
        canonical_payload = {
            'action': action,
            'batch_size': batch_size,
            'pause_seconds': pause_seconds,
            'product_ids': product_ids,
            'source': source,
        }
        request_fingerprint = canonical_payload_fingerprint(canonical_payload)
        try:
            with transaction.atomic():
                type(request.tenant).objects.select_for_update().only('pk').get(
                    pk=request.tenant.pk,
                )
                job = ProductBulkActionJob.objects.filter(
                    tenant=request.tenant,
                    idempotency_key=idempotency_key,
                ).first()
                if job is not None:
                    raise_on_fingerprint_conflict(
                        job.request_fingerprint,
                        request_fingerprint,
                    )
                else:
                    job = ProductBulkActionService.create_job(
                        tenant=request.tenant,
                        action=action,
                        product_ids=product_ids,
                        source_id=source,
                        batch_size=batch_size,
                        pause_seconds=pause_seconds,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        request_payload=canonical_payload,
                    )
                    if job.last_dispatched_at is None:
                        from apps.core.dispatch import enqueue_durable_task
                        enqueue_durable_task(
                            'apps.products.tasks.process_bulk_product_action',
                            args=[job.pk],
                            deduplication_key=f'product-bulk:{job.pk}:batch-offset:0',
                            max_run_attempts=4,
                        )
                        job.last_dispatched_at = now()
                        job.save(update_fields=['last_dispatched_at', 'updated_at'])
                    if action == ProductBulkActionJob.Action.FIND_IMAGES:
                        # Charge only resolved tenant-owned products, and only
                        # for the first canonical intent. A rejected budget
                        # rolls the whole DB transaction back before publish.
                        consume_transactional_tenant_daily_budget(
                            tenant=request.tenant,
                            scope='image-search-jobs',
                            cost=job.total_count,
                            limit=settings.IMAGE_SEARCH_TENANT_DAILY_JOBS,
                        )
                    if (
                        action in {
                            ProductBulkActionJob.Action.ENRICH_SELECTED,
                            ProductBulkActionJob.Action.ENRICH_MISSING_DATA,
                            ProductBulkActionJob.Action.ENRICH_THEN_GENERATE,
                            ProductBulkActionJob.Action.GENERATE_DESCRIPTIONS,
                        }
                    ):
                        # Tachka, Rossko, Euroauto and future parse-capable
                        # sources all cross an external boundary. Description
                        # generation may schedule enrichment later, after the
                        # ingress transaction. Reserve the maximum starts up
                        # front so a source alias or mutable fitment state can
                        # never bypass the tenant ceiling.
                        consume_transactional_tenant_daily_budget(
                            tenant=request.tenant,
                            scope='product-parse-jobs',
                            cost=job.total_count,
                            limit=settings.PRODUCT_PARSE_TENANT_DAILY_JOBS,
                        )
        except IdempotencyConflict as exc:
            return Response(
                {
                    'status': 'error',
                    'code': 'idempotency_conflict',
                    'message': str(exc),
                },
                status=status.HTTP_409_CONFLICT,
            )
        except AutoPartsEnrichmentDisabled as exc:
            return Response(
                {'status': 'error', 'code': 'auto_parts_enrichment_disabled', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except (TypeError, ValueError) as exc:
            return Response(
                {'status': 'error', 'code': 'validation_error', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({
            'status': 'ok',
            'data': ProductBulkActionJobSerializer(job).data,
        }, status=status.HTTP_201_CREATED)


@extend_schema(tags=['Products'])
class ProductBulkActionDetailView(APIView):
    """GET /api/v1/products/bulk-actions/{id}/ — статус bulk job."""

    @extend_schema(responses=ProductBulkActionResponseSerializer)
    def get(self, request, pk: int):
        job = get_object_or_404(ProductBulkActionJob, pk=pk, tenant=request.tenant)
        return Response({'status': 'ok', 'data': ProductBulkActionJobSerializer(job).data})


def _review_actor(request):
    return human_user_or_none(request)


def _set_review_state(obj, request, review_status: str) -> None:
    obj.review_status = review_status
    obj.needs_review = False
    obj.reviewed_at = now()
    obj.reviewed_by = _review_actor(request)
    obj.save(update_fields=['review_status', 'needs_review', 'reviewed_at', 'reviewed_by', 'updated_at'])


def _review_fact_with_feed_fence(
    fact: ProductEnrichmentFact,
    request,
    review_status: str,
) -> ProductEnrichmentFact:
    """Apply a BRAND/OEM review under account -> product -> fact locks."""

    from apps.products.feed_writers import (
        StaleProductFeedWrite,
        capture_product_feed_generation,
        locked_product_feed_write,
    )

    current_product = Product.all_objects.get(pk=fact.product_id)
    generation = capture_product_feed_generation(current_product)
    changes_feed = (
        review_status == ReviewStatus.APPROVED
        and fact.fact_type in {
            ProductEnrichmentFact.FactType.BRAND,
            ProductEnrichmentFact.FactType.OEM,
        }
    )

    expected_updated_at = fact.updated_at
    with locked_product_feed_write(
        (generation,),
        bump_product_ids=(current_product.pk,) if changes_feed else (),
    ) as products:
        locked_fact = ProductEnrichmentFact.objects.select_for_update().filter(
            pk=fact.pk,
            product_id=current_product.pk,
            tenant_id=fact.tenant_id,
        ).first()
        if locked_fact is None or locked_fact.updated_at != expected_updated_at:
            raise StaleProductFeedWrite(
                f'Enrichment fact {fact.pk} changed before review.',
            )
        _set_review_state(locked_fact, request, review_status)
        if review_status == ReviewStatus.APPROVED:
            ProductEnrichmentService.apply_approved_fact(
                cast(Product, products[current_product.pk]),
                locked_fact,
            )
        return locked_fact


def _review_fitment_with_feed_fence(
    fitment: VehicleFitment,
    request,
    review_status: str,
) -> VehicleFitment:
    """Review and refresh denormalized Product fields under Product fencing."""

    from apps.products.feed_writers import (
        StaleProductFeedWrite,
        capture_product_feed_generation,
        locked_product_feed_write,
    )

    current_product = Product.all_objects.get(pk=fitment.product_id)
    generation = capture_product_feed_generation(current_product)
    expected_updated_at = fitment.updated_at
    with locked_product_feed_write((generation,)) as products:
        locked_fitment = VehicleFitment.objects.select_for_update().filter(
            pk=fitment.pk,
            product_id=current_product.pk,
            tenant_id=fitment.tenant_id,
        ).first()
        if locked_fitment is None or locked_fitment.updated_at != expected_updated_at:
            raise StaleProductFeedWrite(
                f'Fitment {fitment.pk} changed before review.',
            )
        locked_product = cast(Product, products[current_product.pk])
        _set_review_state(locked_fitment, request, review_status)
        if review_status == ReviewStatus.APPROVED:
            ProductKnowledgeGraphService.learn_approved_fitment(
                locked_product,
                locked_fitment,
            )
        # Fitment itself is not emitted in XML, but this refresh also writes
        # Product.oem_numbers from ProductCrossCode. Conservatively bump and
        # serialize it with every covered OEM writer.
        ProductEnrichmentService.refresh_product_denormalized_enrichment(
            locked_product,
        )
        locked_product.save(update_fields=[
            'oem_numbers', 'cross_numbers', 'applicability', 'updated_at',
        ])
        return locked_fitment


def _sync_web_research_review(obj, review_status: str) -> None:
    if getattr(obj, 'source_id', '') == 'web_research':
        from apps.web_research.services import WebResearchService
        WebResearchService.record_claim_review(obj, review_status)


def _review_product_payload(product) -> dict[str, Any]:
    return {
        'id': product.pk,
        'article': product.article,
        'name': product.name,
        'brand': product.brand,
        'category_1c': product.category_1c,
        'catalog_category_id': product.catalog_category_id,
    }


def _serialize_review_item(item_type: str, obj: Any) -> ReviewQueueItem:
    product = obj.product
    if item_type == 'fitment':
        title = ' '.join(
            part for part in [obj.make, obj.model, obj.generation, obj.modification]
            if part
        )
        source_id = obj.source_id
        payload = VehicleFitmentSerializer(obj).data
        reason = obj.raw_text or title
    elif item_type == 'fact':
        title = f'{obj.name}: {obj.value}'
        source_id = obj.source_id
        payload = ProductEnrichmentFactSerializer(obj).data
        reason = obj.raw_text or obj.value
    else:
        title = dict(ProductCatalogClassification.Domain.choices).get(obj.domain, obj.domain)
        source_id = obj.source
        payload = ProductCatalogClassificationSerializer(obj).data
        reason = obj.reason
    return {
        'id': f'{item_type}:{obj.pk}',
        'type': item_type,
        'record_id': obj.pk,
        'product': _review_product_payload(product),
        'title': title,
        'reason': reason,
        'source_id': source_id,
        'confidence': obj.confidence,
        'needs_review': obj.needs_review,
        'review_status': obj.review_status,
        'created_at': obj.created_at,
        'updated_at': obj.updated_at,
        'payload': payload,
    }


def _review_queue_search_filter(search: str) -> Q:
    return (
        Q(product__article__icontains=search)
        | Q(product__name__icontains=search)
        | Q(product__brand__icontains=search)
    )


def _bad_review_action_response():
    return Response({'status': 'error', 'code': 'bad_action'}, status=status.HTTP_404_NOT_FOUND)


@extend_schema(tags=['Products'])
class ProductReviewQueueView(APIView):
    """GET /api/v1/products/review-queue/ — единая очередь спорных enrichment-данных."""

    VALID_TYPES = {'fitment', 'fact', 'classification'}

    @extend_schema(
        parameters=[
            OpenApiParameter(
                'type',
                OpenApiTypes.STR,
                enum=['fitment', 'fact', 'classification'],
            ),
            OpenApiParameter('product_id', OpenApiTypes.INT),
            OpenApiParameter('source_id', OpenApiTypes.STR),
            OpenApiParameter('search', OpenApiTypes.STR),
            OpenApiParameter('page', OpenApiTypes.INT),
            OpenApiParameter('page_size', OpenApiTypes.INT),
        ],
        responses=ReviewQueueResponseSerializer,
    )
    def get(self, request):
        item_type = request.query_params.get('type', '').strip()
        if item_type and item_type not in self.VALID_TYPES:
            return Response(
                {'status': 'error', 'code': 'bad_type'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_product_id = request.query_params.get('product_id', '').strip()
        product_id: int | None = None
        if raw_product_id:
            try:
                product_id = int(raw_product_id)
            except (TypeError, ValueError):
                product_id = None
            if product_id is None or product_id <= 0:
                return Response(
                    {'status': 'error', 'code': 'bad_product_id'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        source_id = request.query_params.get('source_id', '').strip()
        search = request.query_params.get('search', '').strip()

        ref_querysets: list[Any] = []
        if not item_type or item_type == 'fitment':
            fitment_qs = VehicleFitment.objects.select_related('product').filter(
                tenant=request.tenant,
                needs_review=True,
                review_status=ReviewStatus.PENDING,
            )
            if product_id is not None:
                fitment_qs = fitment_qs.filter(product_id=product_id)
            if source_id:
                fitment_qs = fitment_qs.filter(source_id=source_id)
            if search:
                fitment_qs = fitment_qs.filter(_review_queue_search_filter(search))
            ref_querysets.append(fitment_qs.annotate(
                item_type=Value('fitment', output_field=CharField()),
                record_id=F('pk'),
            ).values('updated_at', 'item_type', 'record_id'))

        if not item_type or item_type == 'fact':
            fact_qs = ProductEnrichmentFact.objects.select_related('product').filter(
                tenant=request.tenant,
                needs_review=True,
                review_status=ReviewStatus.PENDING,
            )
            if product_id is not None:
                fact_qs = fact_qs.filter(product_id=product_id)
            if source_id:
                fact_qs = fact_qs.filter(source_id=source_id)
            if search:
                fact_qs = fact_qs.filter(_review_queue_search_filter(search))
            ref_querysets.append(fact_qs.annotate(
                item_type=Value('fact', output_field=CharField()),
                record_id=F('pk'),
            ).values('updated_at', 'item_type', 'record_id'))

        if not item_type or item_type == 'classification':
            classification_qs = ProductCatalogClassification.objects.select_related(
                'product',
            ).filter(
                tenant=request.tenant,
                needs_review=True,
                review_status=ReviewStatus.PENDING,
            )
            if product_id is not None:
                classification_qs = classification_qs.filter(product_id=product_id)
            if source_id:
                classification_qs = classification_qs.filter(source=source_id)
            if search:
                classification_qs = classification_qs.filter(
                    _review_queue_search_filter(search),
                )
            ref_querysets.append(classification_qs.annotate(
                item_type=Value('classification', output_field=CharField()),
                record_id=F('pk'),
            ).values('updated_at', 'item_type', 'record_id'))

        refs = ref_querysets[0]
        if len(ref_querysets) > 1:
            refs = refs.union(*ref_querysets[1:], all=True)
        refs = refs.order_by('-updated_at', 'item_type', 'record_id')
        paginator = MapPagination()
        page_refs = cast(
            list[ReviewQueueRef] | None,
            paginator.paginate_queryset(refs, request),
        ) or []

        ids_by_type: dict[str, list[int]] = {
            'fitment': [],
            'fact': [],
            'classification': [],
        }
        for ref in page_refs:
            ids_by_type[ref['item_type']].append(ref['record_id'])

        objects_by_ref: dict[tuple[str, int], Any] = {}
        model_by_type: dict[str, Any] = {
            'fitment': VehicleFitment,
            'fact': ProductEnrichmentFact,
            'classification': ProductCatalogClassification,
        }
        for ref_type, record_ids in ids_by_type.items():
            if not record_ids:
                continue
            objects_by_ref.update({
                (ref_type, obj.pk): obj
                for obj in model_by_type[ref_type].objects.select_related('product').filter(
                    tenant=request.tenant,
                    pk__in=record_ids,
                )
            })

        items = [
            _serialize_review_item(
                ref['item_type'],
                objects_by_ref[(ref['item_type'], ref['record_id'])],
            )
            for ref in page_refs
            if (ref['item_type'], ref['record_id']) in objects_by_ref
        ]
        return paginator.get_paginated_response(items)


@extend_schema(tags=['Products'])
class ProductReviewQueueActionView(APIView):
    """POST /api/v1/products/review-queue/{type}/{id}/{approve|reject}/."""

    api_key_scopes = {}

    @extend_schema(request=None, responses=ReviewQueueActionResponseSerializer)
    def post(self, request, item_type: str, record_id: int, action: str):
        if action not in ['approve', 'reject']:
            return _bad_review_action_response()
        if item_type == 'fitment':
            from apps.products.feed_writers import StaleProductFeedWrite

            fitment = get_object_or_404(VehicleFitment, pk=record_id, tenant=request.tenant)
            review_status = ReviewStatus.APPROVED if action == 'approve' else ReviewStatus.REJECTED
            try:
                fitment = _review_fitment_with_feed_fence(
                    fitment,
                    request,
                    review_status,
                )
            except StaleProductFeedWrite:
                return Response(
                    {'status': 'error', 'code': 'concurrent_product_update'},
                    status=status.HTTP_409_CONFLICT,
                )
            _sync_web_research_review(fitment, review_status)
            return Response({'status': 'ok', 'data': _serialize_review_item(item_type, fitment)})
        if item_type == 'fact':
            from apps.products.feed_writers import StaleProductFeedWrite

            fact = get_object_or_404(
                ProductEnrichmentFact,
                pk=record_id,
                tenant=request.tenant,
            )
            review_status = ReviewStatus.APPROVED if action == 'approve' else ReviewStatus.REJECTED
            try:
                fact = _review_fact_with_feed_fence(
                    fact,
                    request,
                    review_status,
                )
            except StaleProductFeedWrite:
                return Response(
                    {'status': 'error', 'code': 'concurrent_product_update'},
                    status=status.HTTP_409_CONFLICT,
                )
            _sync_web_research_review(fact, review_status)
            return Response({'status': 'ok', 'data': _serialize_review_item(item_type, fact)})
        if item_type == 'classification':
            product = get_object_or_404(
                Product,
                tenant=request.tenant,
                catalog_classification__pk=record_id,
            )
            view = ProductCatalogClassificationReviewView()
            return view.post(request, product.pk, action)
        return Response({'status': 'error', 'code': 'bad_type'}, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Products'])
class ProductFitmentsView(APIView):
    """GET /api/v1/products/{id}/fitments/ — применяемость товара."""

    api_key_enabled = True

    @extend_schema(responses=FitmentListResponseSerializer)
    def get(self, request, pk: int):
        product = get_object_or_404(Product, pk=pk, tenant=request.tenant)
        fitments = product.fitments.filter(tenant=request.tenant).order_by('make', 'model')
        return Response({'status': 'ok', 'data': VehicleFitmentSerializer(fitments, many=True).data})


@extend_schema(tags=['Products'])
class ProductFitmentReviewView(APIView):
    """POST /api/v1/products/{id}/fitments/{fitment_id}/{approve|reject}/."""

    api_key_scopes = {}

    @extend_schema(request=None, responses=FitmentResponseSerializer)
    def post(self, request, pk: int, fitment_id: int, action: str):
        from apps.products.feed_writers import StaleProductFeedWrite

        product = get_object_or_404(Product, pk=pk, tenant=request.tenant)
        fitment = get_object_or_404(VehicleFitment, pk=fitment_id, tenant=request.tenant, product=product)
        if action == 'approve':
            review_status = ReviewStatus.APPROVED
        elif action == 'reject':
            review_status = ReviewStatus.REJECTED
        else:
            return Response({'status': 'error', 'code': 'bad_action'}, status=status.HTTP_404_NOT_FOUND)

        try:
            fitment = _review_fitment_with_feed_fence(
                fitment,
                request,
                review_status,
            )
        except StaleProductFeedWrite:
            return Response(
                {'status': 'error', 'code': 'concurrent_product_update'},
                status=status.HTTP_409_CONFLICT,
            )
        _sync_web_research_review(fitment, fitment.review_status)
        return Response({'status': 'ok', 'data': VehicleFitmentSerializer(fitment).data})


@extend_schema(tags=['Products'])
class ProductEnrichmentFactsView(APIView):
    """GET /api/v1/products/{id}/enrichment-facts/ — факты обогащения товара."""

    api_key_enabled = True

    @extend_schema(responses=EnrichmentFactListResponseSerializer)
    def get(self, request, pk: int):
        product = get_object_or_404(Product, pk=pk, tenant=request.tenant)
        facts = product.enrichment_facts.filter(tenant=request.tenant).order_by('fact_type', 'name')
        return Response({'status': 'ok', 'data': ProductEnrichmentFactSerializer(facts, many=True).data})


@extend_schema(tags=['Products'])
class ProductEnrichmentFactReviewView(APIView):
    """POST /api/v1/products/{id}/enrichment-facts/{fact_id}/{approve|reject}/."""

    api_key_scopes = {}

    @extend_schema(request=None, responses=EnrichmentFactResponseSerializer)
    def post(self, request, pk: int, fact_id: int, action: str):
        from apps.products.feed_writers import StaleProductFeedWrite

        product = get_object_or_404(Product, pk=pk, tenant=request.tenant)
        fact = get_object_or_404(ProductEnrichmentFact, pk=fact_id, tenant=request.tenant, product=product)
        if action == 'approve':
            try:
                fact = _review_fact_with_feed_fence(
                    fact,
                    request,
                    ReviewStatus.APPROVED,
                )
            except StaleProductFeedWrite:
                return Response(
                    {'status': 'error', 'code': 'concurrent_product_update'},
                    status=status.HTTP_409_CONFLICT,
                )
        elif action == 'reject':
            try:
                fact = _review_fact_with_feed_fence(
                    fact,
                    request,
                    ReviewStatus.REJECTED,
                )
            except StaleProductFeedWrite:
                return Response(
                    {'status': 'error', 'code': 'concurrent_product_update'},
                    status=status.HTTP_409_CONFLICT,
                )
        else:
            return Response({'status': 'error', 'code': 'bad_action'}, status=status.HTTP_404_NOT_FOUND)
        _sync_web_research_review(fact, fact.review_status)
        return Response({'status': 'ok', 'data': ProductEnrichmentFactSerializer(fact).data})


@extend_schema(tags=['Products'])
class ProductCatalogClassificationReviewView(APIView):
    """POST /api/v1/products/{id}/catalog-classification/{approve|reject}/."""

    api_key_scopes = {}

    @extend_schema(request=None, responses=ProductDetailResponseSerializer)
    def post(self, request, pk: int, action: str):
        product = get_object_or_404(Product, pk=pk, tenant=request.tenant)
        classification = ProductEnrichmentService.get_or_classify_product_catalog_domain(product)
        if action == 'approve':
            if classification.domain == ProductCatalogClassification.Domain.UNKNOWN:
                return Response(
                    {
                        'status': 'error',
                        'code': 'unknown_classification',
                        'message': 'Нельзя одобрить неопределённый домен. Выберите категорию каталога.',
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            classification.review_status = ReviewStatus.APPROVED
            classification.needs_review = False
            classification.reviewed_at = now()
            classification.reviewed_by = _review_actor(request)
            classification.source = ProductCatalogClassification.Source.MANUAL
            classification.save(update_fields=[
                'review_status', 'needs_review', 'reviewed_at', 'reviewed_by', 'source', 'updated_at',
            ])
        elif action == 'reject':
            classification.review_status = ReviewStatus.REJECTED
            classification.needs_review = False
            classification.reviewed_at = now()
            classification.reviewed_by = _review_actor(request)
            classification.source = ProductCatalogClassification.Source.MANUAL
            classification.domain = ProductCatalogClassification.Domain.UNKNOWN
            classification.confidence = 0
            classification.reason = 'Оператор отклонил автоматическую классификацию.'
            classification.save(update_fields=[
                'review_status', 'needs_review', 'reviewed_at', 'reviewed_by', 'source',
                'domain', 'confidence', 'reason', 'updated_at',
            ])
        else:
            return Response({'status': 'error', 'code': 'bad_action'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'status': 'ok', 'data': ProductDetailSerializer(product, context={'request': request}).data})


@extend_schema(tags=['Products'])
class ProductCrossCodesView(APIView):
    """GET /api/v1/products/{id}/cross-codes/ — OEM/cross-коды товара."""

    api_key_enabled = True

    @extend_schema(responses=CrossCodeListResponseSerializer)
    def get(self, request, pk: int):
        product = get_object_or_404(Product, pk=pk, tenant=request.tenant)
        cross_codes = product.cross_codes.filter(tenant=request.tenant).order_by('manufacturer', 'code')
        return Response({'status': 'ok', 'data': ProductCrossCodeSerializer(cross_codes, many=True).data})


@extend_schema(tags=['Products'])
class ProductPublishView(APIView):
    """POST /api/v1/products/{pk}/publish/ — подготовить листинги для выбранных аккаунтов."""

    api_key_enabled = True
    api_key_scopes = {'POST': {'catalog:write', 'listings:write'}}

    @extend_schema(
        request=ProductMarketplaceTargetsRequestSerializer,
        responses=ProductPublishResponseSerializer,
    )
    def post(self, request, pk):
        """Делегирует публикацию ListingService.publish_product."""
        from apps.marketplaces.provider_registry import ProviderCapabilityUnavailable
        from apps.marketplaces.services import InvalidMarketplaceTargets, ListingService

        serializer = ProductMarketplaceTargetsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            product = Product.objects.get(pk=pk, tenant=request.tenant)
        except Product.DoesNotExist:
            return Response(
                {'status': 'error', 'code': 'not_found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            listing_ids = ListingService.publish_product(
                product,
                request.tenant,
                serializer.validated_data['account_ids'],
            )
        except InvalidMarketplaceTargets as exc:
            return Response(
                {'status': 'error', 'code': 'invalid_account_targets', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ProviderCapabilityUnavailable as exc:
            return Response(
                {'status': 'error', 'code': 'provider_capability_unavailable', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({'status': 'ok', 'data': {'listing_ids': listing_ids}})


@extend_schema(tags=['Products'])
class ProductArchiveView(APIView):
    """POST /api/v1/products/{pk}/archive/ — снять листинги выбранных аккаунтов."""

    api_key_enabled = True
    api_key_scopes = {'POST': {'catalog:write', 'listings:write'}}

    @extend_schema(
        request=ProductMarketplaceTargetsRequestSerializer,
        responses=ProductArchiveResponseSerializer,
    )
    def post(self, request, pk):
        """Делегирует архивацию ListingService.archive_product."""
        from apps.marketplaces.provider_registry import ProviderCapabilityUnavailable
        from apps.marketplaces.services import InvalidMarketplaceTargets, ListingService

        serializer = ProductMarketplaceTargetsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            product = Product.objects.get(pk=pk, tenant=request.tenant)
        except Product.DoesNotExist:
            return Response(
                {'status': 'error', 'code': 'not_found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            count = ListingService.archive_product(
                product,
                request.tenant,
                serializer.validated_data['account_ids'],
            )
        except InvalidMarketplaceTargets as exc:
            return Response(
                {'status': 'error', 'code': 'invalid_account_targets', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ProviderCapabilityUnavailable as exc:
            return Response(
                {'status': 'error', 'code': 'provider_capability_unavailable', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if count == 0:
            return Response(
                {'status': 'error', 'code': 'no_active_listings',
                 'message': 'Нет активных объявлений для архивации. '
                            'Товар уже снят с публикации или никогда не публиковался.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({'status': 'ok', 'data': {'archived_count': count}})


@extend_schema(tags=['Products'])
class ProductRegenerateView(APIView):
    """POST /api/v1/products/{pk}/regenerate/ — enrichment-aware генерация AI-описания."""

    api_key_enabled = True
    api_key_scopes = {'POST': {
        'catalog:write', 'listings:write', 'ai:run',
        'research:run', 'media:write',
    }}
    throttle_classes = [PrincipalScopedRateThrottle, TenantScopedRateThrottle]
    principal_throttle_scope = 'expensive_research_principal'
    tenant_throttle_scope = 'expensive_research_tenant'
    expensive_throttle_methods = {'POST'}

    @extend_schema(
        request=ProductRegenerateRequestSerializer,
        responses={
            202: ProductRegenerateResponseSerializer,
            400: ProductRegenerateErrorSerializer,
            402: ProductRegenerateErrorSerializer,
            404: ProductRegenerateErrorSerializer,
            409: ProductRegenerateErrorSerializer,
        },
    )
    def post(self, request, pk):
        """Сначала обогащает товар, затем запускает генерацию описания."""
        from apps.billing.services import LimitChecker
        from apps.core.dispatch import enqueue_durable_task
        from apps.core.models import BackgroundJobDispatch

        request_payload = ProductRegenerateRequestSerializer(data=request.data)
        request_payload.is_valid(raise_exception=True)
        request_data = request_payload.validated_data

        source = str(request_data.get('source') or DEFAULT_PART_SOURCE).strip()
        intent_key = str(request_data['idempotency_key'])

        # The paid-ingress transaction below locks Product for its own CAS.
        # Materialize a missing category/classification first so its
        # account-first writer can never be entered after that Product lock.
        if (
            request.tenant.supports_auto_parts_enrichment
            and request.tenant.requires_product_auto_parts_check
            and not ProductCatalogClassification.objects.filter(
                product_id=pk,
                tenant=request.tenant,
            ).exists()
        ):
            product_for_classification = get_object_or_404(
                Product,
                pk=pk,
                tenant=request.tenant,
            )
            ProductEnrichmentService.classify_product_catalog_domain(
                product_for_classification,
            )

        with transaction.atomic():
            type(request.tenant).objects.select_for_update().only('pk').get(
                pk=request.tenant.pk,
            )
            try:
                product = Product.objects.select_for_update().get(
                    pk=pk,
                    tenant=request.tenant,
                )
            except Product.DoesNotExist:
                return Response(
                    {'status': 'error', 'code': 'not_found'},
                    status=status.HTTP_404_NOT_FOUND,
                )

            dispatch_prefix = (
                f'product-regenerate:{request.tenant.pk}:{product.pk}:{intent_key}'
            )
            canonical_intent = {
                'product_id': product.pk,
                'source': source,
            }
            intent_fingerprint = canonical_payload_fingerprint(canonical_intent)
            dispatch_key = f'{dispatch_prefix}:{intent_fingerprint}'
            existing_dispatch = BackgroundJobDispatch.objects.filter(
                deduplication_key__startswith=f'{dispatch_prefix}:',
            ).first()
            if existing_dispatch is not None:
                if existing_dispatch.deduplication_key != dispatch_key:
                    return Response(
                        {'status': 'error', 'code': 'idempotency_conflict'},
                        status=status.HTTP_409_CONFLICT,
                    )
                if existing_dispatch.task_name == 'apps.ai_agent.tasks.generate_description_task':
                    if existing_dispatch.args != [product.pk]:
                        return Response(
                            {'status': 'error', 'code': 'idempotency_conflict'},
                            status=status.HTTP_409_CONFLICT,
                        )
                    product_needs_plain_ai = True
                    job = None
                elif existing_dispatch.task_name == (
                    'apps.products.tasks.parse_single_part_then_generate_description'
                ):
                    product_needs_plain_ai = False
                    try:
                        existing_job_id = int(existing_dispatch.args[0])
                        job = request.tenant.product_parse_jobs.get(
                            pk=existing_job_id,
                            product=product,
                            source_id=source,
                        )
                    except (
                        IndexError,
                        TypeError,
                        ValueError,
                        ProductParseJob.DoesNotExist,
                    ):
                        return Response(
                            {'status': 'error', 'code': 'idempotency_conflict'},
                            status=status.HTTP_409_CONFLICT,
                        )
                else:
                    return Response(
                        {'status': 'error', 'code': 'idempotency_conflict'},
                        status=status.HTTP_409_CONFLICT,
                    )
            else:
                try:
                    get_part_source_config(source)
                except ValueError as exc:
                    return Response(
                        {
                            'status': 'error',
                            'code': 'validation_error',
                            'message': str(exc),
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                product_needs_plain_ai = (
                    not request.tenant.supports_auto_parts_enrichment
                    or (
                        request.tenant.requires_product_auto_parts_check
                        and not ProductEnrichmentService.is_product_auto_part_candidate(product)
                    )
                )
                expected_task_name = (
                    'apps.ai_agent.tasks.generate_description_task'
                    if product_needs_plain_ai
                    else 'apps.products.tasks.parse_single_part_then_generate_description'
                )
                can, reason = LimitChecker().can_generate_ai(request.tenant)
                if not can:
                    return Response(
                        {
                            'status': 'error',
                            'code': 'quota_exceeded',
                            'message': reason,
                        },
                        status=status.HTTP_402_PAYMENT_REQUIRED,
                    )
                if product_needs_plain_ai:
                    enqueue_durable_task(
                        expected_task_name,
                        args=[product.pk],
                        deduplication_key=dispatch_key,
                        max_run_attempts=4,
                    )
                    job = None
                else:
                    try:
                        ProductEnrichmentService.ensure_product_auto_parts_eligible(
                            request.tenant,
                            product,
                        )
                    except AutoPartsEnrichmentDisabled as exc:
                        return Response(
                            {
                                'status': 'error',
                                'code': 'auto_parts_enrichment_disabled',
                                'message': str(exc),
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    except ProductIsNotAutoPart as exc:
                        return Response(
                            {
                                'status': 'error',
                                'code': 'product_is_not_auto_part',
                                'message': str(exc),
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    job = ProductEnrichmentService.create_parse_job(
                        tenant=request.tenant,
                        product=product,
                        brand=product.brand,
                        article=product.article,
                        normalized_article=normalize_part_code(product.article),
                        source_id=source,
                        fallback_origin_key=dispatch_key,
                    )
                    enqueue_durable_task(
                        expected_task_name,
                        args=[job.pk],
                        deduplication_key=dispatch_key,
                        max_run_attempts=4,
                    )
                    consume_transactional_tenant_daily_budget(
                        tenant=request.tenant,
                        scope='product-parse-jobs',
                        cost=1,
                        limit=settings.PRODUCT_PARSE_TENANT_DAILY_JOBS,
                    )

        if product_needs_plain_ai:
            return Response({
                'status': 'ok',
                'message': 'Запущена генерация описания без автозапчастного обогащения',
                'data': {
                    'job_id': None,
                    'state': 'queued',
                    'generate_after': True,
                },
            }, status=status.HTTP_202_ACCEPTED)

        if job is None:
            raise RuntimeError('Parse job is required for enrichment regeneration.')
        return Response({
            'status': 'ok',
            'message': 'Запущено обогащение, затем генерация описания',
            'data': {
                'job_id': job.pk,
                'state': job.status,
                'generate_after': True,
            },
        }, status=status.HTTP_202_ACCEPTED)
