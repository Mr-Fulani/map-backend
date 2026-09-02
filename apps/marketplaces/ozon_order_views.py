from rest_framework import serializers, status
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema

from apps.marketplaces.models import MarketplaceAccount, OzonFbsPosting
from apps.marketplaces.ozon_orders import OzonOrderSyncError, sync_fbs_orders
from apps.tenants.api_views import CatalogAPIView as APIView


class OzonAutomationSerializer(serializers.Serializer):
    commerce_auto_sync_enabled = serializers.BooleanField(required=False)
    orders_auto_sync_enabled = serializers.BooleanField(required=False)


@extend_schema(tags=['Accounts'])
class OzonFbsOrdersView(APIView):
    api_key_enabled = True
    api_key_scopes = {'GET': {'catalog:read'}, 'POST': {'catalog:read'}}

    def _account(self, request, pk):
        return MarketplaceAccount.objects.filter(
            pk=pk, tenant=request.tenant, marketplace='ozon', is_active=True,
        ).first()

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

    def _account(self, request, pk):
        return MarketplaceAccount.objects.select_related('ozon_profile').filter(
            pk=pk, tenant=request.tenant, marketplace='ozon', is_active=True,
        ).first()

    def get(self, request, pk):
        account = self._account(request, pk)
        if account is None:
            return Response(status=404)
        profile = account.ozon_profile
        return Response({'status': 'ok', 'data': {
            'commerce_auto_sync_enabled': profile.commerce_auto_sync_enabled,
            'orders_auto_sync_enabled': profile.orders_auto_sync_enabled,
        }})

    def patch(self, request, pk):
        serializer = OzonAutomationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = self._account(request, pk)
        if account is None:
            return Response(status=404)
        profile = account.ozon_profile
        if serializer.validated_data.get('commerce_auto_sync_enabled') and not profile.product_write_enabled:
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
        if update_fields:
            profile.save(update_fields=[*update_fields, 'updated_at'])
        return self.get(request, pk)
