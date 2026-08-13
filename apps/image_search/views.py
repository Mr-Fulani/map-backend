"""DRF-views для API управления изображениями товаров."""

from datetime import timedelta

from celery.result import AsyncResult
from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.timezone import now
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from apps.tenants.api_views import MediaAPIView as APIView
from apps.tenants.permissions import TenantAdminPermission
from apps.tenants.principals import human_user_or_none

from apps.core.throttling import (
    PrincipalScopedRateThrottle,
    TenantScopedRateThrottle,
    consume_transactional_tenant_daily_budget,
)
from apps.core.idempotency import (
    IdempotencyConflict,
    canonical_payload_fingerprint,
    raise_on_fingerprint_conflict,
)
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


class ImageSearchStartRequestSerializer(serializers.Serializer):
    idempotency_key = serializers.UUIDField(required=True)
    force = serializers.BooleanField(required=False, default=False)


class BulkImageSearchRequestSerializer(serializers.Serializer):
    idempotency_key = serializers.UUIDField(required=True)
    product_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=settings.IMAGE_SEARCH_BULK_MAX_PRODUCTS,
    )


IMAGE_SEARCH_STATUS_DATA = inline_serializer(
    name='ImageSearchStatusData',
    fields={
        'state': serializers.ChoiceField(
            choices=[
                'running',
                'done',
                'failed',
                'reconciliation_required',
            ],
            read_only=True,
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


IMAGE_SEARCH_CONFLICT_RESPONSE = inline_serializer(
    name='ImageSearchConflictResponse',
    fields={
        'status': serializers.CharField(read_only=True),
        'code': serializers.CharField(read_only=True),
        'message': serializers.CharField(read_only=True),
    },
)


def _idempotency_conflict_response(exc: IdempotencyConflict) -> Response:
    return Response(
        {
            'status': 'error',
            'code': 'idempotency_conflict',
            'message': str(exc),
        },
        status=status.HTTP_409_CONFLICT,
    )


def _idempotency_incomplete_response() -> Response:
    return Response(
        {
            'status': 'error',
            'code': 'idempotency_incomplete',
            'message': 'Исходный результат запроса недоступен.',
        },
        status=status.HTTP_409_CONFLICT,
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

    api_key_enabled = True

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

    api_key_enabled = True

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

    api_key_enabled = True
    throttle_classes = [PrincipalScopedRateThrottle, TenantScopedRateThrottle]
    principal_throttle_scope = 'expensive_image_principal'
    tenant_throttle_scope = 'expensive_image_tenant'
    expensive_throttle_methods = {'POST'}

    @extend_schema(
        operation_id='product_image_search_start',
        request=ImageSearchStartRequestSerializer,
        responses={
            200: _ok_response(
                'ProductImageSearchStartResponse',
                inline_serializer(
                    name='ProductImageSearchTask',
                    fields={'task_id': serializers.CharField(read_only=True)},
                ),
            ),
            409: IMAGE_SEARCH_CONFLICT_RESPONSE,
        },
    )
    def post(self, request, product_pk: int):
        """Создаёт durable-задачу и возвращает ID для опроса статуса."""
        from apps.image_search.models import ImageSearchCache, ImageSearchIntent
        from apps.image_search.services.dispatch import create_image_search_task

        request_serializer = ImageSearchStartRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        force = request_serializer.validated_data['force']
        idempotency_key = request_serializer.validated_data['idempotency_key']
        if force and not TenantAdminPermission().has_permission(request, self):
            raise PermissionDenied(TenantAdminPermission.message)

        product = get_object_or_404(Product, pk=product_pk, tenant=request.tenant)
        from apps.image_search.services.pipeline import build_cache_key
        cache_key = build_cache_key(product)
        canonical_payload = {
            'force': force,
            'product_id': product.pk,
        }
        fingerprint = canonical_payload_fingerprint(canonical_payload)
        try:
            with transaction.atomic():
                type(request.tenant).objects.select_for_update().only('pk').get(
                    pk=request.tenant.pk,
                )
                intent, created = ImageSearchIntent.objects.get_or_create(
                    tenant=request.tenant,
                    operation=ImageSearchIntent.Operation.SINGLE,
                    idempotency_key=idempotency_key,
                    defaults={
                        'request_fingerprint': fingerprint,
                        'request_payload': canonical_payload,
                    },
                )
                if created:
                    if force:
                        ImageSearchCache.objects.filter(cache_key=cache_key).delete()
                    task = create_image_search_task(
                        tenant=request.tenant,
                        product=product,
                        intent=intent,
                    )
                    consume_transactional_tenant_daily_budget(
                        tenant=request.tenant,
                        scope='image-search-jobs',
                        cost=1,
                        limit=settings.IMAGE_SEARCH_TENANT_DAILY_JOBS,
                    )
                else:
                    raise_on_fingerprint_conflict(
                        intent.request_fingerprint,
                        fingerprint,
                    )
                    existing_task = intent.tasks.filter(product=product).first()
                    if existing_task is None:
                        return _idempotency_incomplete_response()
                    task = existing_task
        except IdempotencyConflict as exc:
            return _idempotency_conflict_response(exc)
        return Response({'status': 'ok', 'data': {'task_id': task.task_id}})


@extend_schema(tags=['Images'])
class ImageSearchStatusView(APIView):
    """GET /api/v1/products/{product_pk}/images/search/{task_id}/ — статус задачи поиска."""

    api_key_enabled = True

    @extend_schema(
        operation_id='product_image_search_status',
        responses=_ok_response(
            'ProductImageSearchStatusResponse',
            IMAGE_SEARCH_STATUS_DATA,
        ),
    )
    def get(self, request, product_pk: int, task_id: str):
        """Возвращает состояние Celery-задачи и количество сохранённых изображений."""
        from apps.image_search.models import ImageSearchTask

        tracking = get_object_or_404(
            ImageSearchTask.objects.select_related('dispatch'),
            task_id=task_id,
            tenant=request.tenant,
            product_id=product_pk,
        )
        if tracking.status == ImageSearchTask.Status.SUCCEEDED:
            outcome = tracking.result if isinstance(tracking.result, dict) else {}
            if not outcome:
                outcome = {
                    'saved_count': 0,
                    'reason_code': 'completed',
                    'message': 'Поиск фотографий завершён.',
                }
            return Response({
                'status': 'ok',
                'data': {'state': 'done', **outcome},
            })
        if tracking.status == ImageSearchTask.Status.FAILED:
            return Response({
                'status': 'ok',
                'data': {
                    'state': 'failed',
                    'saved_count': 0,
                    'reason_code': tracking.error_code or 'task_failed',
                    'message': (
                        tracking.error_message
                        or 'Поиск фотографий завершился с ошибкой.'
                    ),
                },
            })
        if tracking.status == ImageSearchTask.Status.RECONCILIATION_REQUIRED:
            return Response({
                'status': 'ok',
                'data': {
                    'state': 'reconciliation_required',
                    'saved_count': 0,
                    'reason_code': (
                        tracking.error_code
                        or 'provider_reconciliation_required'
                    ),
                    'message': (
                        tracking.error_message
                        or (
                            'Исход платного запроса требует '
                            'сверки оператором.'
                        )
                    ),
                },
            })
        dispatch = tracking.dispatch
        if dispatch is not None:
            if dispatch.status == dispatch.Status.SUCCEEDED:
                outcome = dispatch.result if isinstance(dispatch.result, dict) else {}
                if not outcome:
                    outcome = {
                        'saved_count': 0,
                        'reason_code': 'completed',
                        'message': 'Поиск фотографий завершён.',
                    }
                return Response({
                    'status': 'ok',
                    'data': {'state': 'done', **outcome},
                })
            if dispatch.status in {dispatch.Status.FAILED, dispatch.Status.CANCELLED}:
                failure = dispatch.result if isinstance(dispatch.result, dict) else {}
                return Response({
                    'status': 'ok',
                    'data': {
                        'state': 'failed',
                        'saved_count': 0,
                        'reason_code': failure.get('reason_code', 'task_failed'),
                        'message': failure.get(
                            'message',
                            'Поиск фотографий завершился с ошибкой.',
                        ),
                    },
                })
            return Response({'status': 'ok', 'data': {'state': 'running'}})

        # Compatibility for tasks created before durable dispatch was deployed.
        result = AsyncResult(task_id)

        state_map = {
            'SUCCESS': 'done',
            'FAILURE': 'failed',
            'REVOKED': 'failed',
        }
        state = state_map.get(result.state, 'running')
        if (
            result.state == 'PENDING'
            and tracking.created_at
            <= now() - timedelta(seconds=settings.CELERY_TASK_TIME_LIMIT + 300)
        ):
            state = 'failed'

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
                'reason_code': (
                    'task_result_lost' if result.state == 'PENDING' else 'task_failed'
                ),
                'message': (
                    'Результат старой задачи недоступен; '
                    'запустите поиск повторно.'
                    if result.state == 'PENDING'
                    else 'Поиск фотографий завершился с ошибкой.'
                ),
            }

        return Response({'status': 'ok', 'data': {'state': state, **outcome}})


@extend_schema(tags=['Images'])
class ImageApproveView(APIView):
    """POST /api/v1/products/{product_pk}/images/{image_pk}/approve/ — одобрить изображение."""

    api_key_scopes = {}

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
        reviewed_by = human_user_or_none(request)
        image = moderation.approve(image, reviewed_by=reviewed_by)
        return Response({'status': 'ok', 'data': ProductImageSerializer(image, context={'request': request}).data})


@extend_schema(tags=['Images'])
class ImageRejectView(APIView):
    """POST /api/v1/products/{product_pk}/images/{image_pk}/reject/ — отклонить изображение."""

    api_key_scopes = {}

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
        reviewed_by = human_user_or_none(request)
        image = moderation.reject(image, reviewed_by=reviewed_by)
        return Response({'status': 'ok', 'data': ProductImageSerializer(image, context={'request': request}).data})


@extend_schema(tags=['Images'])
class ImageSetPrimaryView(APIView):
    """PUT /api/v1/products/{product_pk}/images/{image_pk}/set_primary/ — сделать главным."""

    api_key_enabled = True

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

    api_key_enabled = True
    parser_classes = [MultiPartParser]
    throttle_classes = [PrincipalScopedRateThrottle, TenantScopedRateThrottle]
    principal_throttle_scope = 'image_upload_principal'
    tenant_throttle_scope = 'image_upload_tenant'

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

        if file_obj.size > settings.MAX_IMAGE_UPLOAD_BYTES:
            return Response(
                {'status': 'error', 'code': 'validation_error',
                 'errors': {'image': ['Файл превышает допустимый размер.']}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        raw_bytes = file_obj.read(settings.MAX_IMAGE_UPLOAD_BYTES + 1)
        if len(raw_bytes) > settings.MAX_IMAGE_UPLOAD_BYTES:
            return Response(
                {'status': 'error', 'code': 'validation_error',
                 'errors': {'image': ['Файл превышает допустимый размер.']}},
                status=status.HTTP_400_BAD_REQUEST,
            )
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

    api_key_enabled = True
    throttle_classes = [PrincipalScopedRateThrottle, TenantScopedRateThrottle]
    principal_throttle_scope = 'expensive_image_principal'
    tenant_throttle_scope = 'expensive_image_tenant'
    expensive_throttle_methods = {'POST'}

    @extend_schema(
        operation_id='product_image_bulk_search',
        request=BulkImageSearchRequestSerializer,
        responses={
            200: _ok_response(
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
            409: IMAGE_SEARCH_CONFLICT_RESPONSE,
        },
    )
    def post(self, request):
        """Принимает список product_ids, запускает задачу поиска для каждого.

        Тело запроса:
            {"product_ids": [1, 2, 3]}

        Ответ:
            {"status": "ok", "data": {"task_ids": {1: "abc", 2: "def"}, "count": 2}}
        """
        from apps.image_search.models import ImageSearchIntent
        from apps.image_search.services.dispatch import create_image_search_task

        request_serializer = BulkImageSearchRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        product_ids = request_serializer.validated_data['product_ids']
        idempotency_key = request_serializer.validated_data['idempotency_key']
        canonical_product_ids = sorted(set(product_ids))

        # Tenant isolation — принимаем только товары тенанта
        products = list(
            Product.objects.filter(
                pk__in=canonical_product_ids, tenant=request.tenant,
            ).order_by('pk')
        )
        canonical_payload = {'product_ids': canonical_product_ids}
        fingerprint = canonical_payload_fingerprint(canonical_payload)
        try:
            with transaction.atomic():
                type(request.tenant).objects.select_for_update().only('pk').get(
                    pk=request.tenant.pk,
                )
                intent, created = ImageSearchIntent.objects.get_or_create(
                    tenant=request.tenant,
                    operation=ImageSearchIntent.Operation.BULK,
                    idempotency_key=idempotency_key,
                    defaults={
                        'request_fingerprint': fingerprint,
                        'request_payload': {
                            **canonical_payload,
                            'resolved_product_ids': [product.pk for product in products],
                        },
                    },
                )
                if created:
                    tasks = [
                        create_image_search_task(
                            tenant=request.tenant,
                            product=product,
                            intent=intent,
                        )
                        for product in products
                    ]
                    consume_transactional_tenant_daily_budget(
                        tenant=request.tenant,
                        scope='image-search-jobs',
                        cost=len(products),
                        limit=settings.IMAGE_SEARCH_TENANT_DAILY_JOBS,
                    )
                else:
                    raise_on_fingerprint_conflict(
                        intent.request_fingerprint,
                        fingerprint,
                    )
                    tasks = list(intent.tasks.order_by('product_id'))
                    expected_ids = intent.request_payload.get(
                        'resolved_product_ids',
                    )
                    if (
                        not isinstance(expected_ids, list)
                        or [task.product_id for task in tasks] != expected_ids
                    ):
                        return _idempotency_incomplete_response()
        except IdempotencyConflict as exc:
            return _idempotency_conflict_response(exc)

        task_ids = {task.product_id: task.task_id for task in tasks}

        return Response({
            'status': 'ok',
            'data': {'task_ids': task_ids, 'count': len(task_ids)},
        })


@extend_schema(tags=['Images'])
class ImageQuotaView(APIView):
    """GET /api/v1/images/quota/ — текущая квота Brave Search API."""

    api_key_enabled = True

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
