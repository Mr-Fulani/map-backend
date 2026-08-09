"""DRF-views для API управления изображениями товаров."""

from celery.result import AsyncResult
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.image_search.serializers import ProductImageSerializer
from apps.image_search.services import moderation
from apps.products.models import Product, ProductImage


def _ok_response(name, data):
    """Build the common MAP response envelope for OpenAPI only."""
    return inline_serializer(
        name=name,
        fields={
            'status': serializers.CharField(read_only=True),
            'data': data,
        },
    )


IMAGE_SEARCH_STATUS_DATA = inline_serializer(
    name='ImageSearchStatusData',
    fields={
        'state': serializers.ChoiceField(
            choices=['running', 'done', 'failed'], read_only=True,
        ),
        'saved_count': serializers.IntegerField(required=False),
        'found_count': serializers.IntegerField(required=False),
        'rejected_count': serializers.IntegerField(required=False),
        'eligible_count': serializers.IntegerField(required=False),
        'download_failed_count': serializers.IntegerField(required=False),
        'reason_code': serializers.CharField(required=False),
        'message': serializers.CharField(required=False),
        'sources': serializers.ListField(
            child=serializers.CharField(), required=False,
        ),
        'errors': inline_serializer(
            name='ImageSearchSourceError',
            many=True,
            required=False,
            fields={
                'source_id': serializers.CharField(),
                'code': serializers.CharField(),
                'message': serializers.CharField(),
            },
        ),
        'cached': serializers.BooleanField(required=False),
        'product_image_ids': serializers.ListField(
            child=serializers.IntegerField(), required=False,
        ),
    },
)


def _get_product(product_pk: int, tenant) -> Product:
    """Возвращает Product с проверкой tenant isolation."""
    return get_object_or_404(
        Product.objects.prefetch_related('images'),
        pk=product_pk,
        tenant=tenant,
    )


def _get_image(product: Product, image_pk: int) -> ProductImage:
    """Возвращает ProductImage принадлежащий product."""
    return get_object_or_404(ProductImage, pk=image_pk, product=product)


@extend_schema(tags=['Images'])
class ImageListView(APIView):
    """GET /api/v1/products/{product_pk}/images/ — список изображений товара."""

    @extend_schema(
        operation_id='product_image_list',
        responses=_ok_response(
            'ProductImageListResponse',
            ProductImageSerializer(many=True, read_only=True),
        ),
    )
    def get(self, request, product_pk: int):
        """Возвращает все изображения товара, упорядоченные по позиции."""
        product = _get_product(product_pk, request.tenant)
        images = product.images.exclude(status='rejected').order_by('position')
        return Response({
            'status': 'ok',
            'data': ProductImageSerializer(images, many=True, context={'request': request}).data,
        })


@extend_schema(tags=['Images'])
class ImageDetailView(APIView):
    """GET/DELETE /api/v1/products/{product_pk}/images/{image_pk}/."""

    @extend_schema(
        operation_id='product_image_retrieve',
        responses=_ok_response(
            'ProductImageDetailResponse',
            ProductImageSerializer(read_only=True),
        ),
    )
    def get(self, request, product_pk: int, image_pk: int):
        """Возвращает карточку одного изображения."""
        product = _get_product(product_pk, request.tenant)
        image = _get_image(product, image_pk)
        return Response({'status': 'ok', 'data': ProductImageSerializer(image, context={'request': request}).data})

    @extend_schema(
        operation_id='product_image_delete',
        request=None,
        responses={204: None},
    )
    def delete(self, request, product_pk: int, image_pk: int):
        """Помечает изображение как отклонённое (сохраняет sha256/url для дедупликации)."""
        product = _get_product(product_pk, request.tenant)
        image = _get_image(product, image_pk)
        image.status = ProductImage.Status.REJECTED
        image.save(update_fields=['status'])
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Images'])
class ImageSearchView(APIView):
    """POST /api/v1/products/{product_pk}/images/search/ — запустить поиск изображений."""

    @extend_schema(
        operation_id='product_image_search_start',
        request=None,
        responses=_ok_response(
            'ProductImageSearchStartResponse',
            inline_serializer(
                name='ProductImageSearchTask',
                fields={'task_id': serializers.CharField(read_only=True)},
            ),
        ),
    )
    def post(self, request, product_pk: int):
        """Запускает Celery-задачу поиска изображений. Возвращает task_id для опроса статуса."""
        from apps.image_search.models import ImageSearchCache
        from apps.image_search.tasks import search_images_for_product
        product = get_object_or_404(Product, pk=product_pk, tenant=request.tenant)
        from apps.image_search.services.pipeline import build_cache_key
        cache_key = build_cache_key(product)
        ImageSearchCache.objects.filter(cache_key=cache_key).delete()
        task = search_images_for_product.delay(product.pk)
        return Response({'status': 'ok', 'data': {'task_id': task.id}})


@extend_schema(tags=['Images'])
class ImageSearchStatusView(APIView):
    """GET /api/v1/products/{product_pk}/images/search/{task_id}/ — статус задачи поиска."""

    @extend_schema(
        operation_id='product_image_search_status',
        responses=_ok_response(
            'ProductImageSearchStatusResponse',
            IMAGE_SEARCH_STATUS_DATA,
        ),
    )
    def get(self, request, product_pk: int, task_id: str):
        """Возвращает состояние Celery-задачи и количество сохранённых изображений."""
        get_object_or_404(Product, pk=product_pk, tenant=request.tenant)
        result = AsyncResult(task_id)

        state_map = {
            'SUCCESS': 'done',
            'FAILURE': 'failed',
            'REVOKED': 'failed',
        }
        state = state_map.get(result.state, 'running')

        outcome = {}
        if state == 'done':
            task_result = result.result
            if isinstance(task_result, dict):
                outcome = task_result
            else:
                outcome = {
                    'saved_count': 0,
                    'reason_code': 'completed',
                    'message': 'Поиск фотографий завершён.',
                }
        elif state == 'failed':
            outcome = {
                'saved_count': 0,
                'reason_code': 'task_failed',
                'message': 'Поиск фотографий завершился с ошибкой.',
            }

        return Response({'status': 'ok', 'data': {'state': state, **outcome}})


@extend_schema(tags=['Images'])
class ImageApproveView(APIView):
    """POST /api/v1/products/{product_pk}/images/{image_pk}/approve/ — одобрить изображение."""

    @extend_schema(
        operation_id='product_image_approve',
        request=None,
        responses=_ok_response(
            'ProductImageApproveResponse',
            ProductImageSerializer(read_only=True),
        ),
    )
    def post(self, request, product_pk: int, image_pk: int):
        """Переводит изображение в статус AUTO_APPROVED."""
        product = _get_product(product_pk, request.tenant)
        image = _get_image(product, image_pk)
        reviewed_by = request.user if request.user.is_authenticated else None
        image = moderation.approve(image, reviewed_by=reviewed_by)
        return Response({'status': 'ok', 'data': ProductImageSerializer(image, context={'request': request}).data})


@extend_schema(tags=['Images'])
class ImageRejectView(APIView):
    """POST /api/v1/products/{product_pk}/images/{image_pk}/reject/ — отклонить изображение."""

    @extend_schema(
        operation_id='product_image_reject',
        request=None,
        responses=_ok_response(
            'ProductImageRejectResponse',
            ProductImageSerializer(read_only=True),
        ),
    )
    def post(self, request, product_pk: int, image_pk: int):
        """Переводит изображение в статус REJECTED."""
        product = _get_product(product_pk, request.tenant)
        image = _get_image(product, image_pk)
        reviewed_by = request.user if request.user.is_authenticated else None
        image = moderation.reject(image, reviewed_by=reviewed_by)
        return Response({'status': 'ok', 'data': ProductImageSerializer(image, context={'request': request}).data})


@extend_schema(tags=['Images'])
class ImageSetPrimaryView(APIView):
    """PUT /api/v1/products/{product_pk}/images/{image_pk}/set_primary/ — сделать главным."""

    @extend_schema(
        operation_id='product_image_set_primary',
        request=None,
        responses=_ok_response(
            'ProductImageSetPrimaryResponse',
            ProductImageSerializer(read_only=True),
        ),
    )
    def put(self, request, product_pk: int, image_pk: int):
        """Устанавливает изображение как главное, снимает флаг с остальных."""
        product = _get_product(product_pk, request.tenant)
        image = _get_image(product, image_pk)
        image = moderation.set_primary(image)
        return Response({'status': 'ok', 'data': ProductImageSerializer(image, context={'request': request}).data})


@extend_schema(tags=['Images'])
class ImageUploadView(APIView):
    """POST /api/v1/products/{product_pk}/images/upload/ — загрузить изображение вручную."""

    parser_classes = [MultiPartParser]

    @extend_schema(
        operation_id='product_image_upload',
        request=inline_serializer(
            name='ProductImageUploadRequest',
            fields={'image': serializers.ImageField(write_only=True)},
        ),
        responses={
            201: _ok_response(
                'ProductImageUploadResponse',
                ProductImageSerializer(read_only=True),
            ),
        },
    )
    def post(self, request, product_pk: int):
        """Принимает multipart-файл, обрабатывает через Pillow и сохраняет в S3."""
        product = get_object_or_404(Product, pk=product_pk, tenant=request.tenant)
        file_obj = request.FILES.get('image')
        if not file_obj:
            return Response(
                {'status': 'error', 'code': 'validation_error',
                 'errors': {'image': ['Файл обязателен.']}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_bytes = file_obj.read()
        pi = moderation.upload_image(product, raw_bytes)
        if pi is None:
            return Response(
                {'status': 'error', 'code': 'upload_failed',
                 'message': 'Не удалось загрузить: неверный формат или превышен лимит фото.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {'status': 'ok', 'data': ProductImageSerializer(pi, context={'request': request}).data},
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=['Images'])
class BulkSearchView(APIView):
    """POST /api/v1/images/bulk-search/ — запустить поиск для нескольких товаров."""

    @extend_schema(
        operation_id='product_image_bulk_search',
        request=inline_serializer(
            name='ProductImageBulkSearchRequest',
            fields={
                'product_ids': serializers.ListField(
                    child=serializers.IntegerField(min_value=1),
                ),
            },
        ),
        responses=_ok_response(
            'ProductImageBulkSearchResponse',
            inline_serializer(
                name='ProductImageBulkSearchResult',
                fields={
                    'task_ids': serializers.DictField(
                        child=serializers.CharField(), read_only=True,
                    ),
                    'count': serializers.IntegerField(read_only=True),
                },
            ),
        ),
    )
    def post(self, request):
        """Принимает список product_ids, запускает задачу поиска для каждого.

        Тело запроса:
            {"product_ids": [1, 2, 3]}

        Ответ:
            {"status": "ok", "data": {"task_ids": {1: "abc", 2: "def"}, "count": 2}}
        """
        from apps.image_search.tasks import search_images_for_product

        product_ids = request.data.get('product_ids', [])
        if not isinstance(product_ids, list):
            return Response(
                {'status': 'error', 'code': 'validation_error',
                 'errors': {'product_ids': ['Ожидается список ID.']}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Tenant isolation — принимаем только товары тенанта
        valid_pks = list(
            Product.objects.filter(
                pk__in=product_ids, tenant=request.tenant,
            ).values_list('pk', flat=True)
        )

        task_ids = {}
        for pk in valid_pks:
            task = search_images_for_product.delay(pk)
            task_ids[pk] = task.id

        return Response({
            'status': 'ok',
            'data': {'task_ids': task_ids, 'count': len(task_ids)},
        })


@extend_schema(tags=['Images'])
class ImageQuotaView(APIView):
    """GET /api/v1/images/quota/ — текущая квота Brave Search API."""

    @extend_schema(
        operation_id='image_search_quota_retrieve',
        responses=_ok_response(
            'ImageSearchQuotaResponse',
            inline_serializer(
                name='ImageSearchQuota',
                fields={
                    'source': serializers.CharField(read_only=True),
                    'period': serializers.CharField(read_only=True),
                    'used': serializers.IntegerField(read_only=True),
                    'limit': serializers.IntegerField(read_only=True),
                    'soft_cap': serializers.IntegerField(read_only=True),
                    'is_paused': serializers.BooleanField(read_only=True),
                },
            ),
        ),
    )
    def get(self, request):
        """Возвращает персистентный счётчик запросов Brave за текущий месяц.

        used      — использовано запросов (хранится в БД, не сбрасывается при рестарте)
        limit     — лимит плана (обновляется из заголовков Brave, default 1000)
        soft_cap  — порог отключения (800)
        period    — расчётный месяц YYYY-MM
        is_paused — True если soft cap достигнут и Brave временно отключён
        """
        from apps.image_search.models import BraveQuota
        quota = BraveQuota.current()
        return Response({
            'status': 'ok',
            'data': {
                'source': 'brave',
                'period': quota.period,
                'used': quota.requests_used,
                'limit': quota.limit,
                'soft_cap': BraveQuota.SOFT_CAP,
                'is_paused': quota.requests_used >= BraveQuota.SOFT_CAP,
            },
        })
