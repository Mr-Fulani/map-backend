from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.response import Response

from apps.core.queue_observability import get_cached_celery_queue_snapshot
from apps.marketplaces.ozon_health import health_snapshot, profile_queryset, queue_status, work_snapshot
from apps.tenants.api_views import CatalogAPIView


class OzonHealthView(CatalogAPIView):
    """A tenant/account-fenced local read; never retries commerce or calls Ozon."""

    api_key_enabled = True
    api_key_scopes = {'GET': {'catalog:read'}}

    @extend_schema(
        tags=['Accounts'], operation_id='ozon_account_health',
        responses=inline_serializer(name='OzonHealthResponse', fields={
            'status': serializers.CharField(), 'data': serializers.DictField(),
        }),
    )
    def get(self, request, pk):
        profile = profile_queryset().filter(account_id=pk, account__tenant=request.tenant).first()
        if profile is None:
            return Response(status=404)
        return Response({'status': 'ok', 'data': health_snapshot(
            profile, work=work_snapshot(profile),
            queue=queue_status(get_cached_celery_queue_snapshot()),
        )})
