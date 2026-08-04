from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

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
from apps.media_processing.services import activate_variant, create_processing_job
from apps.products.models import ProductImage


class MediaProviderListView(APIView):
    """Configured capabilities and tariff visibility without exposing API credentials."""

    def get(self, request):
        registered = {provider.provider_id: provider for provider in list_media_providers()}
        policies = {policy.provider_id: policy for policy in MediaProviderPolicy.objects.all()}
        provider_ids = list(dict.fromkeys([*policies, *registered]))
        data = []
        for provider_id in provider_ids:
            provider = registered.get(provider_id)
            policy = policies.get(provider_id)
            data.append({
                'provider_id': provider_id,
                'display_name': (
                    policy.display_name if policy else provider.display_name or provider_id
                ),
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


class MediaPresetListCreateView(APIView):
    def get(self, request):
        presets = MediaProcessingPreset.objects.filter(
            Q(tenant=request.tenant) | Q(tenant__isnull=True),
            is_active=True,
        ).order_by('tenant_id', 'name')
        return Response({
            'status': 'ok',
            'data': MediaProcessingPresetSerializer(presets, many=True).data,
        })

    def post(self, request):
        serializer = MediaProcessingPresetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        preset = serializer.save(tenant=request.tenant)
        return Response(
            {'status': 'ok', 'data': MediaProcessingPresetSerializer(preset).data},
            status=status.HTTP_201_CREATED,
        )


class TenantMediaSettingsView(APIView):
    def get_object(self, request):
        obj, _ = TenantMediaSettings.objects.get_or_create(tenant=request.tenant)
        return obj

    def get(self, request):
        return Response({
            'status': 'ok',
            'data': TenantMediaSettingsSerializer(
                self.get_object(request), context={'request': request},
            ).data,
        })

    def patch(self, request):
        obj = self.get_object(request)
        serializer = TenantMediaSettingsSerializer(
            obj, data=request.data, partial=True, context={'request': request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({'status': 'ok', 'data': serializer.data})


class MediaJobListView(APIView):
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


class MediaJobDetailView(APIView):
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


class ProductImageProcessView(APIView):
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
        preset = None
        if data.get('preset_id'):
            preset = get_object_or_404(
                MediaProcessingPreset,
                Q(tenant=request.tenant) | Q(tenant__isnull=True),
                pk=data['preset_id'],
                is_active=True,
            )
        try:
            job = create_processing_job(
                product_image=image,
                preset=preset,
                operations=data.get('operations'),
                parameters=data.get('parameters'),
                provider_id=data.get('provider_id', ''),
                requested_by=request.user if request.user.is_authenticated else None,
                idempotency_key=data.get('idempotency_key', ''),
            )
        except ValueError as exc:
            raise ValidationError({'operations': [str(exc)]}) from exc
        from apps.media_processing.tasks import process_media_job
        transaction.on_commit(lambda: process_media_job.delay(job.pk))
        return Response(
            {'status': 'ok', 'data': MediaProcessingJobSerializer(job).data},
            status=status.HTTP_202_ACCEPTED,
        )


class ProductImageVariantActivateView(APIView):
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


class ImageAssessmentListView(APIView):
    def get(self, request):
        assessments = ImageAssessment.objects.filter(
            tenant=request.tenant,
        ).select_related('product', 'product_image')[:200]
        return Response({
            'status': 'ok',
            'data': ImageAssessmentSerializer(assessments, many=True).data,
        })
