import uuid

from django.core.files.storage import default_storage
from django.db.models import Count, Prefetch, Q
from django.db import transaction
from drf_spectacular.utils import extend_schema
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.pagination import MapPagination
from apps.products.enrichment import normalize_part_code
from apps.products.models import (
    Product, ProductBulkActionJob, ProductParseJob,
    TenantCatalogCategory, TenantCategoryMapping,
)
from apps.products.serializers import (
    ProductBulkActionJobSerializer, ProductCrossCodeSerializer,
    ProductDetailSerializer, ProductParseJobSerializer, ProductSerializer,
    TenantCatalogCategorySerializer, TenantCategoryMappingSerializer,
    VehicleFitmentSerializer,
)
from apps.products.services import (
    AutoPartsEnrichmentDisabled, ProductBulkActionService, ProductEnrichmentService,
    ProductIsNotAutoPart, ProductService,
)
from apps.products.tasks import import_from_datasource
from apps.products.source_policy import DEFAULT_PART_SOURCE, get_part_source_config


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
            .prefetch_related('images', Prefetch('parse_jobs', queryset=latest_jobs))
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

        export_enabled = request.query_params.get('export_enabled')
        if export_enabled is not None:
            qs = qs.filter(export_enabled=export_enabled.lower() == 'true')

        category = request.query_params.get('category_1c', '').strip()
        if category:
            qs = qs.filter(category_1c=category)

        catalog_domain = request.query_params.get('catalog_domain', '').strip()
        if catalog_domain:
            qs = qs.filter(catalog_classification__domain=catalog_domain)

        catalog_category = request.query_params.get('catalog_category', '').strip()
        if catalog_category:
            qs = qs.filter(catalog_category_id=catalog_category)

        qs = qs.order_by('-sync_at', '-created_at')

        paginator = MapPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(
            ProductSerializer(page, many=True, context={'request': request}).data
        )


@extend_schema(tags=['Products'])
class ProductDetailView(APIView):
    """GET /api/v1/products/{pk}/ — карточка товара."""

    def get(self, request, pk):
        try:
            product = Product.objects.select_related('catalog_category', 'catalog_classification').prefetch_related(
                'images', 'attributes', 'cross_codes', 'fitments', 'parse_jobs',
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
        qs = TenantCatalogCategory.objects.filter(tenant=request.tenant).order_by('name')
        return Response({
            'status': 'ok',
            'data': TenantCatalogCategorySerializer(qs, many=True, context={'request': request}).data,
        })

    def post(self, request):
        serializer = TenantCatalogCategorySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        parent = serializer.validated_data.get('parent')
        if parent is not None and parent.tenant_id != request.tenant.id:
            return Response(
                {'status': 'error', 'code': 'validation_error', 'message': 'Родительская категория другого tenant-а'},
                status=status.HTTP_400_BAD_REQUEST,
            )
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
        parent = serializer.validated_data.get('parent')
        if parent is not None and parent.tenant_id != request.tenant.id:
            return Response(
                {'status': 'error', 'code': 'validation_error', 'message': 'Родительская категория другого tenant-а'},
                status=status.HTTP_400_BAD_REQUEST,
            )
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
        source = str(request.data.get('source') or DEFAULT_PART_SOURCE).strip()
        generate_after = bool(request.data.get('generate_after'))
        try:
            get_part_source_config(source)
        except ValueError as exc:
            return Response(
                {'status': 'error', 'code': 'validation_error', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

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

        try:
            job = ProductEnrichmentService.create_parse_job(
                tenant=request.tenant,
                product=product,
                brand=brand,
                article=article,
                normalized_article=normalize_part_code(article),
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

        from apps.products.tasks import (
            parse_single_part, parse_single_part_then_generate_description,
        )
        task = (
            parse_single_part_then_generate_description
            if generate_after else parse_single_part
        )
        transaction.on_commit(lambda: task.delay(job.pk))

        return Response({
            'status': 'ok',
            'data': {
                'job_id': job.pk,
                'state': job.status,
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


@extend_schema(tags=['Products'])
class ProductFitmentsView(APIView):
    """GET /api/v1/products/{id}/fitments/ — применяемость товара."""

    def get(self, request, pk: int):
        product = get_object_or_404(Product, pk=pk, tenant=request.tenant)
        fitments = product.fitments.filter(tenant=request.tenant).order_by('make', 'model')
        return Response({'status': 'ok', 'data': VehicleFitmentSerializer(fitments, many=True).data})


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
