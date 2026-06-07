"""DRF-views для API управления изображениями товаров."""

from celery.result import AsyncResult
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.image_search.serializers import ProductImageSerializer
from apps.image_search.services import moderation
from apps.products.models import Product, ProductImage


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

    def get(self, request, product_pk: int, image_pk: int):
        """Возвращает карточку одного изображения."""
        product = _get_product(product_pk, request.tenant)
        image = _get_image(product, image_pk)
        return Response({'status': 'ok', 'data': ProductImageSerializer(image, context={'request': request}).data})

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

    def post(self, request, product_pk: int):
        """Запускает Celery-задачу поиска изображений. Возвращает task_id для опроса статуса."""
        from apps.image_search.models import ImageSearchCache
        from apps.image_search.tasks import search_images_for_product
        product = get_object_or_404(Product, pk=product_pk, tenant=request.tenant)
        cache_key = f'img_search:{product.article}:{product.brand}'
        ImageSearchCache.objects.filter(cache_key=cache_key).delete()
        task = search_images_for_product.delay(product.pk)
        return Response({'status': 'ok', 'data': {'task_id': task.id}})


@extend_schema(tags=['Images'])
class ImageSearchStatusView(APIView):
    """GET /api/v1/products/{product_pk}/images/search/{task_id}/ — статус задачи поиска."""

    def get(self, request, product_pk: int, task_id: str):
        """Возвращает состояние Celery-задачи и количество сохранённых изображений."""
        product = get_object_or_404(Product, pk=product_pk, tenant=request.tenant)
        result = AsyncResult(task_id)

        state_map = {
            'SUCCESS': 'done',
            'FAILURE': 'failed',
            'REVOKED': 'failed',
        }
        state = state_map.get(result.state, 'running')

        saved_count = 0
        if state == 'done':
            saved_count = product.images.filter(
                status__in=[
                    ProductImage.Status.AUTO_APPROVED,
                    ProductImage.Status.NEEDS_REVIEW,
                    ProductImage.Status.LOW_CONFIDENCE,
                ]
            ).count()

        return Response({'status': 'ok', 'data': {'state': state, 'saved_count': saved_count}})


@extend_schema(tags=['Images'])
class ImageApproveView(APIView):
    """POST /api/v1/products/{product_pk}/images/{image_pk}/approve/ — одобрить изображение."""

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

    def get(self, request):
        """Возвращает количество вызовов Brave за текущий месяц.

        used — самостоятельно отслеживаемый счётчик запросов к Brave API в текущем месяце.
        limit — лимит плана из заголовков Brave (null если Brave ещё не вызывался).
        """
        from datetime import datetime
        from django.core.cache import cache
        key = f'brave:calls:{datetime.now().strftime("%Y-%m")}'
        used = cache.get(key) or 0
        limit = cache.get('brave:quota:limit')
        return Response({
            'status': 'ok',
            'data': {
                'source': 'brave',
                'used': used,
                'limit': limit,
            },
        })
