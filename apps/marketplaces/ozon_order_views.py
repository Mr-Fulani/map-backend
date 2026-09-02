from django.db import transaction
from rest_framework import serializers, status
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema, inline_serializer

from apps.marketplaces.models import MarketplaceAccount, OzonFbsPosting
from apps.marketplaces.ozon_orders import OzonOrderSyncError, sync_fbs_orders
from apps.marketplaces.ozon_rollout import OZON_PRODUCT_IMPORT_METHOD
from apps.tenants.api_views import CatalogAPIView as APIView


class OzonAutomationSerializer(serializers.Serializer):
    product_write_enabled = serializers.BooleanField(required=False)
    commerce_auto_sync_enabled = serializers.BooleanField(required=False)
    orders_auto_sync_enabled = serializers.BooleanField(required=False)


OZON_FBS_ORDERS_RESPONSE = inline_serializer(
    name='OzonFbsOrdersResponse',
    fields={
        'status': serializers.CharField(read_only=True),
        'data': serializers.ListField(
            child=serializers.DictField(read_only=True),
            read_only=True,
        ),
    },
)

OZON_FBS_ORDER_SYNC_RESPONSE = inline_serializer(
    name='OzonFbsOrderSyncResponse',
    fields={
        'status': serializers.CharField(read_only=True),
        'data': serializers.DictField(read_only=True),
    },
)

OZON_AUTOMATION_RESPONSE = inline_serializer(
    name='OzonAutomationResponse',
    fields={
        'status': serializers.CharField(read_only=True),
        'data': OzonAutomationSerializer(read_only=True),
    },
)


@extend_schema(tags=['Accounts'])
class OzonFbsOrdersView(APIView):
    api_key_enabled = True
    api_key_scopes = {'GET': {'catalog:read'}, 'POST': {'catalog:read'}}

    def _account(self, request, pk):
        return MarketplaceAccount.objects.filter(
            pk=pk, tenant=request.tenant, marketplace='ozon', is_active=True,
        ).first()

    @extend_schema(
        operation_id='ozon_fbs_orders_list',
        responses=OZON_FBS_ORDERS_RESPONSE,
    )
    def get(self, request, pk):
        account = self._account(request, pk)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        rows = OzonFbsPosting.objects.filter(account=account).order_by('-in_process_at', '-id')[:200]
        return Response({'status': 'ok', 'data': [{
            'id': row.pk, 'posting_number': row.posting_number,
            'status': row.status, 'substatus': row.substatus,
            'in_process_at': row.in_process_at, 'shipment_date': row.shipment_date,
            'warehouse_id': row.warehouse_id, 'products': row.products,
            'last_synced_at': row.last_synced_at,
        } for row in rows]})

    @extend_schema(
        operation_id='ozon_fbs_orders_sync',
        request=None,
        responses=OZON_FBS_ORDER_SYNC_RESPONSE,
    )
    def post(self, request, pk):
        account = self._account(request, pk)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            imported = sync_fbs_orders(account)
        except OzonOrderSyncError as exc:
            return Response({'status': 'error', 'code': exc.code, 'message': str(exc)}, status=400)
        return Response({'status': 'ok', 'data': {'imported': imported}})


@extend_schema(tags=['Accounts'])
class OzonAutomationView(APIView):
    """Explicit account-scoped automation switches; both default to off."""

    api_key_enabled = True
    api_key_scopes = {'GET': {'catalog:read'}, 'PATCH': {'catalog:write'}}

    def _account(self, request, pk, *, for_update=False):
        queryset = MarketplaceAccount.objects.filter(
            pk=pk, tenant=request.tenant, marketplace='ozon', is_active=True,
        )
        if for_update:
            return queryset.select_for_update().first()
        return queryset.select_related('ozon_profile').first()

    @extend_schema(
        operation_id='ozon_automation_retrieve',
        responses=OZON_AUTOMATION_RESPONSE,
    )
    def get(self, request, pk):
        account = self._account(request, pk)
        if account is None:
            return Response(status=404)
        profile = account.ozon_profile
        return Response({'status': 'ok', 'data': {
            'product_write_enabled': profile.product_write_enabled,
            'commerce_auto_sync_enabled': profile.commerce_auto_sync_enabled,
            'orders_auto_sync_enabled': profile.orders_auto_sync_enabled,
        }})

    @extend_schema(
        operation_id='ozon_automation_update',
        request=OzonAutomationSerializer,
        responses=OZON_AUTOMATION_RESPONSE,
    )
    def patch(self, request, pk):
        serializer = OzonAutomationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            account = self._account(request, pk, for_update=True)
            if account is None:
                return Response(status=404)
            profile = account.ozon_profile
            profile = type(profile).objects.select_for_update().get(pk=profile.pk)
            enable_write = serializer.validated_data.get('product_write_enabled') is True
            if enable_write and OZON_PRODUCT_IMPORT_METHOD not in profile.api_methods:
                return Response(
                    {
                        'status': 'error',
                        'code': 'product_write_permission_missing',
                        'message': (
                            'У API-ключа нет права Product на создание и изменение товаров. '
                            'Создайте новый ключ Ozon с методом /v3/product/import.'
                        ),
                    },
                    status=400,
                )
            resulting_write = serializer.validated_data.get(
                'product_write_enabled', profile.product_write_enabled,
            )
            if serializer.validated_data.get('commerce_auto_sync_enabled') and not resulting_write:
                return Response(
                    {
                        'status': 'error',
                        'code': 'write_disabled',
                        'message': 'Сначала разрешите запись товаров для этого кабинета.',
                    },
                    status=400,
                )
            update_fields = []
            for field, value in serializer.validated_data.items():
                setattr(profile, field, value)
                update_fields.append(field)
            if serializer.validated_data.get('product_write_enabled') is False:
                profile.commerce_auto_sync_enabled = False
                if 'commerce_auto_sync_enabled' not in update_fields:
                    update_fields.append('commerce_auto_sync_enabled')
            if update_fields:
                profile.save(update_fields=[*update_fields, 'updated_at'])
        return self.get(request, pk)
