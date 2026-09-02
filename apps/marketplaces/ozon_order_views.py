from rest_framework import serializers, status
from rest_framework.response import Response

from apps.marketplaces.models import MarketplaceAccount, OzonFbsPosting
from apps.marketplaces.ozon_orders import OzonOrderSyncError, sync_fbs_orders
from apps.tenants.api_views import CatalogAPIView as APIView


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
        serializer = serializers.Serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = self._account(request, pk)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            imported = sync_fbs_orders(account)
        except OzonOrderSyncError as exc:
            return Response({'status': 'error', 'code': exc.code, 'message': str(exc)}, status=400)
        return Response({'status': 'ok', 'data': {'imported': imported}})
