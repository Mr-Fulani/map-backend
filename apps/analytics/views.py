"""Tenant dashboard read API."""

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.serializers import DashboardSummaryResponseSerializer
from apps.analytics.services import build_tenant_dashboard_summary
from apps.tenants.permissions import HumanUserOnly, TenantRolePermission


@extend_schema(tags=['Dashboard'])
class DashboardSummaryView(APIView):
    """GET /api/v1/dashboard/summary/ — bounded tenant dashboard aggregate."""

    permission_classes = [IsAuthenticated, HumanUserOnly, TenantRolePermission]

    @extend_schema(
        operation_id='tenant_dashboard_summary_retrieve',
        summary='Получить сводку главного дашборда',
        responses=DashboardSummaryResponseSerializer,
    )
    def get(self, request):
        return Response({
            'status': 'ok',
            'data': build_tenant_dashboard_summary(request.tenant),
        })
