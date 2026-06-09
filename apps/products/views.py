import uuid

from django.core.files.storage import default_storage
from django.db.models import Case, Count, F, IntegerField, Prefetch, Q, Subquery, OuterRef, Value, When
from django.db import transaction
from drf_spectacular.utils import extend_schema
from django.shortcuts import get_object_or_404
from django.utils.timezone import now
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.pagination import MapPagination
from apps.products.enrichment import normalize_part_code
from apps.products.models import (
    Product, ProductBulkActionJob, ProductCatalogClassification, ProductEnrichmentFact,
    ProductParseJob, ReviewStatus,
    TenantCatalogCategory, TenantCategoryMapping,
    VehicleFitment,
)
from apps.products.serializers import (
    ProductCatalogClassificationSerializer,
    ProductBulkActionJobSerializer, ProductCrossCodeSerializer, ProductEnrichmentFactSerializer,
    ProductDetailSerializer, ProductParseJobSerializer, ProductSerializer,
    TenantCatalogCategorySerializer, TenantCategoryMappingSerializer,
    VehicleFitmentSerializer,
)
from apps.products.services import (
    AutoPartsEnrichmentDisabled, ProductBulkActionService, ProductEnrichmentService,
    ProductIsNotAutoPart, ProductService,
)
from apps.marketplaces.models import Listing
from apps.products.tasks import import_from_datasource
from apps.products.source_policy import DEFAULT_PART_SOURCE, get_part_source_config, get_part_source_policies
from apps.tenants.models import CatalogDomain, TenantCatalogDomain


def _validate_tenant_catalog_category(request, serializer):
    root_domain = serializer.validated_data.get('root_domain')
    parent = serializer.validated_data.get('parent')
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
        elif listing_filter in ('active', 'pending', 'queued', 'requires_review', 'limit_reached', 'rejected', 'draft', 'archived', 'deleted'):
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

    def get(self, request, pk):
        try:
            product = Product.objects.select_related('catalog_category', 'catalog_classification').prefetch_related(
                'images', 'attributes', 'cross_codes', 'fitments', 'enrichment_facts', 'parse_jobs',
            ).get(
                pk=pk, tenant=request.tenant
            )
        except Product.DoesNotExist:
            return Response(
                {'status': 'error', 'code': 'not_found', 'message': 'Товар не найден'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({'status': 'ok', 'data': ProductDetailSerializer(product, context={'request': request}).data})


class ProductSyncView(APIView):
    """POST /api/v1/products/sync/{connection_id}/ — запустить импорт товаров."""

    def post(self, request, connection_id):
        from apps.datasources.models import DataSourceConnection
        from django.shortcuts import get_object_or_404
        conn = get_object_or_404(DataSourceConnection, pk=connection_id, tenant=request.tenant)
        task = import_from_datasource.delay(conn.pk)
        return Response({'status': 'ok', 'data': {'task_id': task.id}})


@extend_schema(tags=['Catalog Categories'])
class TenantCatalogCategoryListView(APIView):
    """GET/POST /api/v1/products/catalog-categories/."""

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
        return Response({
            'status': 'ok',
            'data': TenantCatalogCategorySerializer(qs, many=True, context={'request': request}).data,
        })

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

    def get(self, request, pk):
        category = get_object_or_404(TenantCatalogCategory, pk=pk, tenant=request.tenant)
        serializer = TenantCatalogCategorySerializer(category, context={'request': request})
        return Response({'status': 'ok', 'data': serializer.data})

    def put(self, request, pk):
        category = get_object_or_404(TenantCatalogCategory, pk=pk, tenant=request.tenant)
        serializer = TenantCatalogCategorySerializer(category, data=request.data)
        serializer.is_valid(raise_exception=True)
        validation_error = _validate_tenant_catalog_category(request, serializer)
        if validation_error is not None:
            return validation_error
        category = serializer.save(tenant=request.tenant)
        serializer = TenantCatalogCategorySerializer(category, context={'request': request})
        return Response({'status': 'ok', 'data': serializer.data})

    def delete(self, request, pk):
        category = get_object_or_404(TenantCatalogCategory, pk=pk, tenant=request.tenant)
        category.is_active = False
        category.save(update_fields=['is_active', 'updated_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Catalog Categories'])
class TenantCatalogCategoryDefaultImageView(APIView):
    """POST /api/v1/products/catalog-categories/{id}/default-image/ — загрузить fallback-картинку."""

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, pk):
        category = get_object_or_404(TenantCatalogCategory, pk=pk, tenant=request.tenant)
        image = request.FILES.get('image')
        if image is None:
            return Response(
                {'status': 'error', 'code': 'validation_error', 'message': 'Передайте файл image'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if image.size > 5 * 1024 * 1024:
            return Response(
                {'status': 'error', 'code': 'validation_error', 'message': 'Размер файла не должен превышать 5 МБ'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        content_type = getattr(image, 'content_type', '')
        if content_type and not content_type.startswith('image/'):
            return Response(
                {'status': 'error', 'code': 'validation_error', 'message': 'Можно загрузить только изображение'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        extension = (image.name.rsplit('.', 1)[-1] if '.' in image.name else 'jpg').lower()
        if extension not in ['jpg', 'jpeg', 'png', 'webp']:
            extension = 'jpg'
        s3_key = f'catalog-categories/{request.tenant.slug}/{category.pk}/{uuid.uuid4().hex}.{extension}'
        if category.default_image_s3_key:
            default_storage.delete(category.default_image_s3_key)
        saved_key = default_storage.save(s3_key, image)
        category.default_image_s3_key = saved_key
        category.default_image_source_name = image.name[:255]
        category.save(update_fields=[
            'default_image_s3_key', 'default_image_source_name', 'updated_at',
        ])
        serializer = TenantCatalogCategorySerializer(category, context={'request': request})
        return Response({'status': 'ok', 'data': serializer.data})

    def delete(self, request, pk):
        category = get_object_or_404(TenantCatalogCategory, pk=pk, tenant=request.tenant)
        if category.default_image_s3_key:
            default_storage.delete(category.default_image_s3_key)
        category.default_image_s3_key = ''
        category.default_image_source_name = ''
        category.save(update_fields=[
            'default_image_s3_key', 'default_image_source_name', 'updated_at',
        ])
        serializer = TenantCatalogCategorySerializer(category, context={'request': request})
        return Response({'status': 'ok', 'data': serializer.data})


@extend_schema(tags=['Catalog Categories'])
class TenantCategoryMappingListView(APIView):
    """GET/POST /api/v1/products/catalog-category-mappings/."""

    def get(self, request):
        qs = (
            TenantCategoryMapping.objects
            .filter(tenant=request.tenant)
            .select_related('category')
            .order_by('source_category')
        )
        return Response({'status': 'ok', 'data': TenantCategoryMappingSerializer(qs, many=True).data})

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

    def delete(self, request, pk):
        mapping = get_object_or_404(TenantCategoryMapping, pk=pk, tenant=request.tenant)
        mapping.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Catalog Categories'])
class TenantSourceCategoryListView(APIView):
    """GET /api/v1/products/catalog-source-categories/."""

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

    def post(self, request):
        product_ids = request.data.get('product_ids') or []
        category_id = request.data.get('catalog_category')
        if not isinstance(product_ids, list) or not product_ids:
            return Response(
                {'status': 'error', 'code': 'validation_error', 'message': 'Выберите товары для назначения категории'},
                status=status.HTTP_400_BAD_REQUEST,
            )

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

        valid_ids = list(
            Product.objects
            .filter(tenant=request.tenant, pk__in=product_ids)
            .values_list('pk', flat=True)
        )
        skipped_count = max(len(set(product_ids)) - len(valid_ids), 0)
        with transaction.atomic():
            Product.objects.filter(tenant=request.tenant, pk__in=valid_ids).update(catalog_category=category)
            products = (
                Product.objects
                .filter(tenant=request.tenant, pk__in=valid_ids)
                .select_related('tenant', 'catalog_category')
            )
            for product in products:
                ProductEnrichmentService.classify_product_catalog_domain(product, force=True)

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

    def post(self, request):
        """Исключает товары из синхронизации с источником (1С/CSV)."""
        product_ids = request.data.get('product_ids') or []
        exclude = request.data.get('exclude', True)
        if not isinstance(product_ids, list) or not product_ids:
            return Response(
                {'status': 'error', 'code': 'validation_error', 'message': 'Выберите товары'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        updated = Product.objects.filter(
            tenant=request.tenant, pk__in=product_ids,
        ).update(sync_excluded=bool(exclude))
        return Response({'status': 'ok', 'data': {'updated_count': updated}})


@extend_schema(tags=['Products'])
class ProductSearchView(APIView):
    """GET /api/v1/products/search/?brand=&article= — поиск товара tenant-а."""

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


@extend_schema(tags=['Products'])
class ProductParseView(APIView):
    """POST /api/v1/products/parse/ — поставить enrichment job в очередь."""

    def post(self, request):
        product_id = request.data.get('product_id')
        brand = str(request.data.get('brand') or '').strip()
        article = str(request.data.get('article') or '').strip()
        explicit_source = str(request.data.get('source') or '').strip()
        generate_after = bool(request.data.get('generate_after'))

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

        from apps.products.tasks import (
            parse_single_part, parse_single_part_then_generate_description,
        )

        jobs = []
        try:
            for src in sources:
                job = ProductEnrichmentService.create_parse_job(
                    tenant=request.tenant,
                    product=product,
                    brand=brand,
                    article=article,
                    normalized_article=normalize_part_code(article),
                    source_id=src,
                )
                jobs.append(job)
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

        policies = get_part_source_policies()
        primary_job = max(jobs, key=lambda j: policies[j.source_id].priority)

        for job in jobs:
            is_primary = job.pk == primary_job.pk
            task = (
                parse_single_part_then_generate_description
                if (generate_after and is_primary) else parse_single_part
            )
            transaction.on_commit(lambda pk=job.pk, t=task: t.delay(pk))

        return Response({
            'status': 'ok',
            'data': {
                'job_id': primary_job.pk,
                'state': primary_job.status,
                'generate_after': generate_after,
            },
        }, status=status.HTTP_201_CREATED)


@extend_schema(tags=['Products'])
class ProductParseJobDetailView(APIView):
    """GET /api/v1/products/parse-jobs/{id}/ — статус enrichment job."""

    def get(self, request, pk: int):
        job = get_object_or_404(ProductParseJob, pk=pk, tenant=request.tenant)
        return Response({'status': 'ok', 'data': ProductParseJobSerializer(job).data})


@extend_schema(tags=['Products'])
class ProductBulkActionView(APIView):
    """POST /api/v1/products/bulk-actions/ — throttled массовое действие."""

    def post(self, request):
        action = request.data.get('action')
        product_ids = request.data.get('product_ids', [])
        source = str(request.data.get('source') or DEFAULT_PART_SOURCE).strip()
        batch_size = int(request.data.get('batch_size') or 20)
        pause_seconds = int(request.data.get('pause_seconds') or 60)

        if not isinstance(product_ids, list):
            return Response(
                {'status': 'error', 'code': 'validation_error',
                 'errors': {'product_ids': ['Ожидается список ID.']}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            job = ProductBulkActionService.create_job(
                tenant=request.tenant,
                action=action,
                product_ids=[int(pk) for pk in product_ids],
                source_id=source,
                batch_size=batch_size,
                pause_seconds=pause_seconds,
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

        from apps.products.tasks import process_bulk_product_action
        transaction.on_commit(lambda: process_bulk_product_action.delay(job.pk))

        return Response({
            'status': 'ok',
            'data': ProductBulkActionJobSerializer(job).data,
        }, status=status.HTTP_201_CREATED)


@extend_schema(tags=['Products'])
class ProductBulkActionDetailView(APIView):
    """GET /api/v1/products/bulk-actions/{id}/ — статус bulk job."""

    def get(self, request, pk: int):
        job = get_object_or_404(ProductBulkActionJob, pk=pk, tenant=request.tenant)
        return Response({'status': 'ok', 'data': ProductBulkActionJobSerializer(job).data})


def _review_actor(request):
    return request.user if getattr(request.user, 'is_authenticated', False) else None


def _set_review_state(obj, request, review_status: str) -> None:
    obj.review_status = review_status
    obj.needs_review = False
    obj.reviewed_at = now()
    obj.reviewed_by = _review_actor(request)
    obj.save(update_fields=['review_status', 'needs_review', 'reviewed_at', 'reviewed_by', 'updated_at'])


def _review_product_payload(product) -> dict:
    return {
        'id': product.pk,
        'article': product.article,
        'name': product.name,
        'brand': product.brand,
        'category_1c': product.category_1c,
    }


def _serialize_review_item(item_type: str, obj) -> dict:
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

    def get(self, request):
        item_type = request.query_params.get('type', '').strip()
        if item_type and item_type not in self.VALID_TYPES:
            return Response(
                {'status': 'error', 'code': 'bad_type'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        product_id = request.query_params.get('product_id', '').strip()
        source_id = request.query_params.get('source_id', '').strip()
        search = request.query_params.get('search', '').strip()

        items = []
        if not item_type or item_type == 'fitment':
            qs = VehicleFitment.objects.select_related('product').filter(
                tenant=request.tenant,
                needs_review=True,
                review_status=ReviewStatus.PENDING,
            )
            if product_id:
                qs = qs.filter(product_id=product_id)
            if source_id:
                qs = qs.filter(source_id=source_id)
            if search:
                qs = qs.filter(_review_queue_search_filter(search))
            items.extend(_serialize_review_item('fitment', obj) for obj in qs)

        if not item_type or item_type == 'fact':
            qs = ProductEnrichmentFact.objects.select_related('product').filter(
                tenant=request.tenant,
                needs_review=True,
                review_status=ReviewStatus.PENDING,
            )
            if product_id:
                qs = qs.filter(product_id=product_id)
            if source_id:
                qs = qs.filter(source_id=source_id)
            if search:
                qs = qs.filter(_review_queue_search_filter(search))
            items.extend(_serialize_review_item('fact', obj) for obj in qs)

        if not item_type or item_type == 'classification':
            qs = ProductCatalogClassification.objects.select_related('product').filter(
                tenant=request.tenant,
                needs_review=True,
                review_status=ReviewStatus.PENDING,
            )
            if product_id:
                qs = qs.filter(product_id=product_id)
            if source_id:
                qs = qs.filter(source=source_id)
            if search:
                qs = qs.filter(_review_queue_search_filter(search))
            items.extend(_serialize_review_item('classification', obj) for obj in qs)

        items.sort(key=lambda item: item['updated_at'], reverse=True)
        paginator = MapPagination()
        page = paginator.paginate_queryset(items, request)
        return paginator.get_paginated_response(page)


@extend_schema(tags=['Products'])
class ProductReviewQueueActionView(APIView):
    """POST /api/v1/products/review-queue/{type}/{id}/{approve|reject}/."""

    def post(self, request, item_type: str, record_id: int, action: str):
        if action not in ['approve', 'reject']:
            return _bad_review_action_response()
        if item_type == 'fitment':
            item = get_object_or_404(VehicleFitment, pk=record_id, tenant=request.tenant)
            review_status = ReviewStatus.APPROVED if action == 'approve' else ReviewStatus.REJECTED
            _set_review_state(item, request, review_status)
            ProductEnrichmentService.refresh_product_denormalized_enrichment(item.product)
            item.product.save(update_fields=['oem_numbers', 'cross_numbers', 'applicability', 'updated_at'])
            return Response({'status': 'ok', 'data': _serialize_review_item(item_type, item)})
        if item_type == 'fact':
            item = get_object_or_404(ProductEnrichmentFact, pk=record_id, tenant=request.tenant)
            review_status = ReviewStatus.APPROVED if action == 'approve' else ReviewStatus.REJECTED
            _set_review_state(item, request, review_status)
            return Response({'status': 'ok', 'data': _serialize_review_item(item_type, item)})
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

    def get(self, request, pk: int):
        product = get_object_or_404(Product, pk=pk, tenant=request.tenant)
        fitments = product.fitments.filter(tenant=request.tenant).order_by('make', 'model')
        return Response({'status': 'ok', 'data': VehicleFitmentSerializer(fitments, many=True).data})


@extend_schema(tags=['Products'])
class ProductFitmentReviewView(APIView):
    """POST /api/v1/products/{id}/fitments/{fitment_id}/{approve|reject}/."""

    def post(self, request, pk: int, fitment_id: int, action: str):
        product = get_object_or_404(Product, pk=pk, tenant=request.tenant)
        fitment = get_object_or_404(VehicleFitment, pk=fitment_id, tenant=request.tenant, product=product)
        if action == 'approve':
            _set_review_state(fitment, request, ReviewStatus.APPROVED)
        elif action == 'reject':
            _set_review_state(fitment, request, ReviewStatus.REJECTED)
        else:
            return Response({'status': 'error', 'code': 'bad_action'}, status=status.HTTP_404_NOT_FOUND)

        ProductEnrichmentService.refresh_product_denormalized_enrichment(product)
        product.save(update_fields=['oem_numbers', 'cross_numbers', 'applicability', 'updated_at'])
        return Response({'status': 'ok', 'data': VehicleFitmentSerializer(fitment).data})


@extend_schema(tags=['Products'])
class ProductEnrichmentFactsView(APIView):
    """GET /api/v1/products/{id}/enrichment-facts/ — факты обогащения товара."""

    def get(self, request, pk: int):
        product = get_object_or_404(Product, pk=pk, tenant=request.tenant)
        facts = product.enrichment_facts.filter(tenant=request.tenant).order_by('fact_type', 'name')
        return Response({'status': 'ok', 'data': ProductEnrichmentFactSerializer(facts, many=True).data})


@extend_schema(tags=['Products'])
class ProductEnrichmentFactReviewView(APIView):
    """POST /api/v1/products/{id}/enrichment-facts/{fact_id}/{approve|reject}/."""

    def post(self, request, pk: int, fact_id: int, action: str):
        product = get_object_or_404(Product, pk=pk, tenant=request.tenant)
        fact = get_object_or_404(ProductEnrichmentFact, pk=fact_id, tenant=request.tenant, product=product)
        if action == 'approve':
            _set_review_state(fact, request, ReviewStatus.APPROVED)
        elif action == 'reject':
            _set_review_state(fact, request, ReviewStatus.REJECTED)
        else:
            return Response({'status': 'error', 'code': 'bad_action'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'status': 'ok', 'data': ProductEnrichmentFactSerializer(fact).data})


@extend_schema(tags=['Products'])
class ProductCatalogClassificationReviewView(APIView):
    """POST /api/v1/products/{id}/catalog-classification/{approve|reject}/."""

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

    def get(self, request, pk: int):
        product = get_object_or_404(Product, pk=pk, tenant=request.tenant)
        cross_codes = product.cross_codes.filter(tenant=request.tenant).order_by('manufacturer', 'code')
        return Response({'status': 'ok', 'data': ProductCrossCodeSerializer(cross_codes, many=True).data})


@extend_schema(tags=['Products'])
class ProductPublishView(APIView):
    """POST /api/v1/products/{pk}/publish/ — создать/обновить листинги для всех аккаунтов тенанта."""

    def post(self, request, pk):
        """Делегирует публикацию ListingService.publish_product."""
        from apps.marketplaces.services import ListingService, NoActiveAccounts

        try:
            product = Product.objects.get(pk=pk, tenant=request.tenant)
        except Product.DoesNotExist:
            return Response(
                {'status': 'error', 'code': 'not_found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            listing_ids = ListingService.publish_product(product, request.tenant)
        except NoActiveAccounts:
            return Response(
                {'status': 'error', 'code': 'no_accounts',
                 'message': 'Нет подключённых аккаунтов. Добавьте аккаунт в настройках.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({'status': 'ok', 'data': {'listing_ids': listing_ids}})


@extend_schema(tags=['Products'])
class ProductArchiveView(APIView):
    """POST /api/v1/products/{pk}/archive/ — снять все активные листинги товара с публикации."""

    def post(self, request, pk):
        """Делегирует архивацию ListingService.archive_product."""
        from apps.marketplaces.services import ListingService

        try:
            product = Product.objects.get(pk=pk, tenant=request.tenant)
        except Product.DoesNotExist:
            return Response(
                {'status': 'error', 'code': 'not_found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        count = ListingService.archive_product(product, request.tenant)
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

    def post(self, request, pk):
        """Сначала обогащает товар, затем запускает генерацию описания."""
        from apps.billing.services import LimitChecker

        try:
            product = Product.objects.get(pk=pk, tenant=request.tenant)
        except Product.DoesNotExist:
            return Response(
                {'status': 'error', 'code': 'not_found'},
                status=status.HTTP_404_NOT_FOUND,
            )

        can, reason = LimitChecker().can_generate_ai(request.tenant)
        if not can:
            return Response(
                {'status': 'error', 'code': 'quota_exceeded', 'message': reason},
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )

        source = str(request.data.get('source') or DEFAULT_PART_SOURCE).strip()
        try:
            get_part_source_config(source)
        except ValueError as exc:
            return Response(
                {'status': 'error', 'code': 'validation_error', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        product_needs_plain_ai = (
            not request.tenant.supports_auto_parts_enrichment
            or (
                request.tenant.requires_product_auto_parts_check
                and not ProductEnrichmentService.is_product_auto_part_candidate(product)
            )
        )
        if product_needs_plain_ai:
            ProductService.schedule_ai_generation(product, request.tenant)
            return Response({
                'status': 'ok',
                'message': 'Запущена генерация описания без автозапчастного обогащения',
                'data': {
                    'job_id': None,
                    'state': 'queued',
                    'generate_after': True,
                },
            }, status=status.HTTP_202_ACCEPTED)

        try:
            job = ProductEnrichmentService.create_parse_job(
                tenant=request.tenant,
                product=product,
                brand=product.brand,
                article=product.article,
                normalized_article=normalize_part_code(product.article),
                source_id=source,
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

        from apps.products.tasks import parse_single_part_then_generate_description
        transaction.on_commit(lambda: parse_single_part_then_generate_description.delay(job.pk))

        return Response({
            'status': 'ok',
            'message': 'Запущено обогащение, затем генерация описания',
            'data': {
                'job_id': job.pk,
                'state': job.status,
                'generate_after': True,
            },
        }, status=status.HTTP_202_ACCEPTED)
