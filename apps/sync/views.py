import re
from typing import Protocol, cast

from drf_spectacular.utils import extend_schema
from rest_framework.generics import ListAPIView
from rest_framework.exceptions import ValidationError

from apps.sync.models import SyncLog
from apps.sync.serializers import SyncLogSerializer
from apps.tenants.models import Tenant


class _TenantRequest(Protocol):
    tenant: Tenant


@extend_schema(tags=['Logs'], summary='Журнал синхронизации')
class SyncLogListView(ListAPIView):
    """
    GET /api/v1/logs/ — список SyncLog тенанта с фильтрацией и пагинацией.

    Фильтры (query params): event_type/operation, status/provider_result,
    marketplace, account, date (YYYY-MM-DD).
    Результаты отсортированы по убыванию даты создания.
    """

    serializer_class = SyncLogSerializer
    api_key_enabled = True
    api_key_scopes = {
        'GET': {'sync:read'},
        'HEAD': {'sync:read'},
        'OPTIONS': {'sync:read'},
    }

    def get_queryset(self):
        tenant = cast(_TenantRequest, self.request).tenant
        qs = (
            SyncLog.objects.filter(tenant=tenant)
            .select_related('listing__account')
            .order_by('-created_at')
        )

        event_type = (
            self.request.query_params.get('operation')
            or self.request.query_params.get('event_type')
        )
        if event_type:
            qs = qs.filter(event_type=event_type)

        log_status = (
            self.request.query_params.get('provider_result')
            or self.request.query_params.get('status')
        )
        if log_status:
            qs = qs.filter(status=log_status)

        marketplace = self.request.query_params.get('marketplace', '').strip().lower()
        if marketplace:
            if len(marketplace) > 50 or re.fullmatch(r'[a-z0-9_-]+', marketplace) is None:
                raise ValidationError({
                    'marketplace': 'Некорректный идентификатор маркетплейса.',
                })
            qs = qs.filter(
                listing__tenant=tenant,
                listing__account__tenant=tenant,
                listing__account__marketplace=marketplace,
            )

        account_id = self.request.query_params.get('account', '').strip()
        if account_id:
            if (
                not account_id.isascii()
                or not account_id.isdecimal()
                or int(account_id) < 1
            ):
                raise ValidationError({'account': 'Ожидается положительный ID.'})
            qs = qs.filter(
                listing__tenant=tenant,
                listing__account__tenant=tenant,
                listing__account_id=int(account_id),
            )

        date = self.request.query_params.get('date')
        if date:
            qs = qs.filter(created_at__date=date)

        return qs
