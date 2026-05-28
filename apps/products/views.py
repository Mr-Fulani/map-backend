from django.db.models import Count, Prefetch, Q
from django.db import transaction
from drf_spectacular.utils import extend_schema
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.pagination import MapPagination
from apps.products.enrichment import normalize_part_code
from apps.products.models import Product, ProductBulkActionJob, ProductParseJob
from apps.products.serializers import (
    ProductBulkActionJobSerializer, ProductCrossCodeSerializer,
    ProductDetailSerializer, ProductParseJobSerializer, ProductSerializer,
    VehicleFitmentSerializer,
)
from apps.products.services import ProductBulkActionService, ProductEnrichmentService
from apps.products.tasks import import_from_datasource
from apps.products.part_parsers import DEFAULT_PART_SOURCE, get_part_source_config


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
            product = Product.objects.prefetch_related(
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
        ).prefetch_related('images').first()
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

        job = ProductEnrichmentService.create_parse_job(
            tenant=request.tenant,
            product=product,
            brand=brand,
            article=article,
            normalized_article=normalize_part_code(article),
            source_id=source,
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

        job = ProductEnrichmentService.create_parse_job(
            tenant=request.tenant,
            product=product,
            brand=product.brand,
            article=product.article,
            normalized_article=normalize_part_code(product.article),
            source_id=source,
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
