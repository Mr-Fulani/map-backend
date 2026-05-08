from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import Invoice, Plan
from apps.billing.serializers import InvoiceSerializer, PlanSerializer, SubscriptionSerializer
from apps.billing.services import LimitChecker


class PlanListView(APIView):
    """GET /api/v1/billing/plans/ — список доступных тарифов."""

    permission_classes = []   # публичный эндпоинт

    def get(self, request):
        plans = Plan.objects.filter(is_active=True)
        return Response({
            'status': 'ok',
            'data': PlanSerializer(plans, many=True).data,
        })


class SubscriptionView(APIView):
    """GET /api/v1/billing/subscription/ — текущая подписка тенанта."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            sub = request.tenant.subscription
        except Exception:
            return Response({'status': 'ok', 'data': None})

        return Response({
            'status': 'ok',
            'data': SubscriptionSerializer(sub).data,
        })


class UsageView(APIView):
    """GET /api/v1/billing/usage/ — использование лимитов тенантом."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        summary = LimitChecker().get_usage_summary(request.tenant)
        return Response({'status': 'ok', 'data': summary})


class InvoiceListView(APIView):
    """GET /api/v1/billing/invoices/ — история платежей."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        invoices = Invoice.objects.filter(tenant=request.tenant).order_by('-created_at')
        return Response({
            'status': 'ok',
            'data': InvoiceSerializer(invoices, many=True).data,
        })
