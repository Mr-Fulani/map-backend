from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from apps.tenants.api_views import MediaAPIView as APIView
from apps.tenants.permissions import TenantAdminWritePermission
from apps.tenants.principals import human_user_or_none

from apps.media_processing.models import (
    ImageAssessment,
    MediaProcessingJob,
    MediaProcessingPreset,
    MediaProviderPolicy,
    ProductImageVariant,
    TenantMediaSettings,
)
from apps.media_processing.providers.registry import list_media_providers
from apps.media_processing.serializers import (
    ImageAssessmentSerializer,
    MediaJobCreateSerializer,
    MediaProcessingJobSerializer,
    MediaProcessingPresetSerializer,
    ProductImageVariantSerializer,
    TenantMediaSettingsSerializer,
)
from apps.media_processing.services import (
    activate_variant,
    create_processing_job,
    media_processing_request_fingerprint,
)
from apps.core.idempotency import (
    IdempotencyConflict,
    raise_on_fingerprint_conflict,
)
from apps.products.models import ProductImage


def _ok_response(name, data):
    """Build the common MAP response envelope for OpenAPI only."""
    return inline_serializer(
        name=name,
        fields={
            'status': serializers.CharField(read_only=True),
            'data': data,
        },
    )


MEDIA_PROVIDER_RESPONSE = inline_serializer(
    name='MediaProvider',
    fields={
        'provider_id': serializers.CharField(read_only=True),
        'display_name': serializers.CharField(read_only=True),
        'is_active': serializers.BooleanField(read_only=True),
        'is_configured': serializers.BooleanField(read_only=True),
        'capabilities': serializers.ListField(
            child=serializers.CharField(), read_only=True,
        ),
        'priority': serializers.IntegerField(read_only=True),
        'allowed_plan_slugs': serializers.ListField(
            child=serializers.CharField(), read_only=True,
        ),
        'operation_credit_costs': serializers.DictField(read_only=True),
    },
)


@extend_schema(tags=['Media processing'])
class MediaProviderListView(APIView):
    """Configured capabilities and tariff visibility without exposing API credentials."""

    api_key_enabled = True

    @extend_schema(
        operation_id='media_provider_list',
        responses=_ok_response(
            'MediaProviderListResponse',
            MEDIA_PROVIDER_RESPONSE.__class__(many=True, read_only=True),
        ),
    )
    def get(self, request):
        registered = {provider.provider_id: provider for provider in list_media_providers()}
        policies = {policy.provider_id: policy for policy in MediaProviderPolicy.objects.all()}
        provider_ids = list(dict.fromkeys([*policies, *registered]))
        data = []
        for provider_id in provider_ids:
            provider = registered.get(provider_id)
            policy = policies.get(provider_id)
            if policy is not None:
                display_name = policy.display_name
            elif provider is not None:
                display_name = provider.display_name or provider_id
            else:
                # ``provider_ids`` is built from these two mappings, but keep
                # the response deterministic if that implementation changes.
                display_name = provider_id
            data.append({
                'provider_id': provider_id,
                'display_name': display_name,
                'is_active': policy.is_active if policy else True,
                'is_configured': provider.is_configured() if provider else False,
                'capabilities': (
                    policy.capabilities if policy and policy.capabilities
                    else sorted(operation.value for operation in provider.supported_operations)
                    if provider else []
                ),
                'priority': policy.priority if policy else 100,
                'allowed_plan_slugs': policy.allowed_plan_slugs if policy else [],
                'operation_credit_costs': policy.operation_credit_costs if policy else {},
            })
        return Response({'status': 'ok', 'data': data})


@extend_schema(tags=['Media processing'])
class MediaPresetListCreateView(APIView):
    api_key_enabled = True

    @extend_schema(
        operation_id='media_preset_list',
        summary='Получить пресеты обработки медиа',
        responses=_ok_response(
            'MediaPresetListResponse',
            MediaProcessingPresetSerializer(many=True, read_only=True),
        ),
    )
    def get(self, request):
        presets = MediaProcessingPreset.objects.filter(
            Q(tenant=request.tenant) | Q(tenant__isnull=True),
            is_active=True,
        ).order_by('tenant_id', 'name')
        return Response({
            'status': 'ok',
            'data': MediaProcessingPresetSerializer(presets, many=True).data,
        })

    @extend_schema(
        operation_id='media_preset_create',
        summary='Создать пресет обработки медиа',
        request=MediaProcessingPresetSerializer,
        responses={
            201: _ok_response(
                'MediaPresetCreateResponse',
                MediaProcessingPresetSerializer(read_only=True),
            ),
        },
    )
    def post(self, request):
        serializer = MediaProcessingPresetSerializer(
            data=request.data,
            context={'request': request},
        )
        serializer.is_valid(raise_exception=True)
        preset = serializer.save(tenant=request.tenant)
        return Response(
            {'status': 'ok', 'data': MediaProcessingPresetSerializer(preset).data},
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=['Media processing'])
class TenantMediaSettingsView(APIView):
    # Tenant policy is a human-admin concern, not a machine integration API.
    permission_classes = [IsAuthenticated, TenantAdminWritePermission]
    api_key_scopes = {}

    def get_object(self, request):
        obj, _ = TenantMediaSettings.objects.get_or_create(tenant=request.tenant)
        return obj

    @extend_schema(
        operation_id='media_settings_retrieve',
        summary='Получить настройки обработки медиа',
        responses=_ok_response(
            'TenantMediaSettingsResponse',
            TenantMediaSettingsSerializer(read_only=True),
        ),
    )
    def get(self, request):
        return Response({
            'status': 'ok',
            'data': TenantMediaSettingsSerializer(
                self.get_object(request), context={'request': request},
            ).data,
        })

    @extend_schema(
        operation_id='media_settings_update',
        summary='Обновить настройки обработки медиа',
        request=TenantMediaSettingsSerializer,
        responses=_ok_response(
            'TenantMediaSettingsUpdateResponse',
            TenantMediaSettingsSerializer(read_only=True),
        ),
    )
    def patch(self, request):
        obj = self.get_object(request)
        serializer = TenantMediaSettingsSerializer(
            obj, data=request.data, partial=True, context={'request': request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({'status': 'ok', 'data': serializer.data})


@extend_schema(tags=['Media processing'])
class MediaJobListView(APIView):
    api_key_enabled = True

    @extend_schema(
        operation_id='media_job_list',
        summary='Получить задания обработки медиа',
        responses=_ok_response(
            'MediaJobListResponse',
            MediaProcessingJobSerializer(many=True, read_only=True),
        ),
    )
    def get(self, request):
        jobs = MediaProcessingJob.objects.filter(tenant=request.tenant).select_related(
            'product_image__product', 'preset',
        ).prefetch_related('variants')[:200]
        return Response({
            'status': 'ok',
            'data': MediaProcessingJobSerializer(
                jobs, many=True, context={'request': request},
            ).data,
        })


@extend_schema(tags=['Media processing'])
class MediaJobDetailView(APIView):
    api_key_enabled = True

    @extend_schema(
        operation_id='media_job_retrieve',
        summary='Получить задание обработки медиа',
        responses=_ok_response(
            'MediaJobDetailResponse',
            MediaProcessingJobSerializer(read_only=True),
        ),
    )
    def get(self, request, job_pk: int):
        job = get_object_or_404(
            MediaProcessingJob.objects.prefetch_related('variants'),
            pk=job_pk,
            tenant=request.tenant,
        )
        return Response({
            'status': 'ok',
            'data': MediaProcessingJobSerializer(job, context={'request': request}).data,
        })


@extend_schema(tags=['Media processing'])
class ProductImageProcessView(APIView):
    api_key_enabled = True

    @extend_schema(
        operation_id='product_image_process',
        summary='Запустить обработку изображения товара',
        request=MediaJobCreateSerializer,
        responses={
            202: _ok_response(
                'ProductImageProcessResponse',
                MediaProcessingJobSerializer(read_only=True),
            ),
            409: inline_serializer(
                name='ProductImageProcessConflictResponse',
                fields={
                    'status': serializers.CharField(read_only=True),
                    'code': serializers.CharField(read_only=True),
                    'message': serializers.CharField(read_only=True),
                },
            ),
        },
    )
    def post(self, request, product_pk: int, image_pk: int):
        image = get_object_or_404(
            ProductImage.objects.select_related('product__tenant'),
            pk=image_pk,
            product_id=product_pk,
            product__tenant=request.tenant,
        )
        payload = MediaJobCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        try:
            with transaction.atomic():
                type(request.tenant).objects.select_for_update().only('pk').get(
                    pk=request.tenant.pk,
                )
                idempotency_key = str(data['idempotency_key'])
                request_fingerprint = media_processing_request_fingerprint(
                    product_image_id=image.pk,
                    preset_id=data.get('preset_id'),
                    operations=data.get('operations'),
                    parameters=data.get('parameters'),
                    provider_id=data.get('provider_id', ''),
                )
                job = MediaProcessingJob.objects.filter(
                    tenant=request.tenant,
                    idempotency_key=idempotency_key,
                ).first()
                if job is not None:
                    raise_on_fingerprint_conflict(
                        job.request_fingerprint,
                        request_fingerprint,
                    )
                    created_for_request = False
                else:
                    preset = None
                    if data.get('preset_id'):
                        preset = get_object_or_404(
                            MediaProcessingPreset,
                            Q(tenant=request.tenant) | Q(tenant__isnull=True),
                            pk=data['preset_id'],
                            is_active=True,
                        )
                    job = create_processing_job(
                        product_image=image,
                        preset=preset,
                        operations=data.get('operations'),
                        parameters=data.get('parameters'),
                        provider_id=data.get('provider_id', ''),
                        requested_by=human_user_or_none(request),
                        idempotency_key=idempotency_key,
                    )
                    created_for_request = bool(
                        getattr(job, '_created_for_request', False),
                    )
                retryable_submission = (
                    job.status == MediaProcessingJob.Status.FAILED
                    and job.error_code == 'submission_failed'
                )
                if created_for_request or retryable_submission:
                    from apps.core.dispatch import enqueue_durable_task
                    enqueue_durable_task(
                        'apps.media_processing.tasks.process_media_job',
                        args=[job.pk],
                        deduplication_key=f'media-processing-job:{job.pk}',
                        max_run_attempts=4,
                        revive_failed=retryable_submission,
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
        except ValueError as exc:
            raise ValidationError({'operations': [str(exc)]}) from exc
        return Response(
            {'status': 'ok', 'data': MediaProcessingJobSerializer(job).data},
            status=status.HTTP_202_ACCEPTED,
        )


@extend_schema(tags=['Media processing'])
class ProductImageVariantActivateView(APIView):
    api_key_enabled = True

    @extend_schema(
        operation_id='media_variant_activate',
        summary='Активировать вариант изображения',
        request=None,
        responses=_ok_response(
            'ProductImageVariantActivateResponse',
            ProductImageVariantSerializer(read_only=True),
        ),
    )
    def post(self, request, variant_pk: int):
        variant = get_object_or_404(
            ProductImageVariant.objects.select_related('product_image__product'),
            pk=variant_pk,
            tenant=request.tenant,
        )
        activate_variant(variant)
        return Response({
            'status': 'ok',
            'data': ProductImageVariantSerializer(
                variant, context={'request': request},
            ).data,
        })


@extend_schema(tags=['Media processing'])
class ImageAssessmentListView(APIView):
    api_key_enabled = True

    @extend_schema(
        operation_id='media_assessment_list',
        summary='Получить оценки изображений',
        responses=_ok_response(
            'ImageAssessmentListResponse',
            ImageAssessmentSerializer(many=True, read_only=True),
        ),
    )
    def get(self, request):
        assessments = ImageAssessment.objects.filter(
            tenant=request.tenant,
        ).select_related('product', 'product_image')[:200]
        return Response({
            'status': 'ok',
            'data': ImageAssessmentSerializer(assessments, many=True).data,
        })
