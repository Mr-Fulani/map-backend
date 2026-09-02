"""Bounded read-only synchronization of Ozon FBS orders."""

from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.marketplaces.adapters.ozon.client import OzonAPIError, OzonSellerClient
from apps.marketplaces.models import MarketplaceAccount, OzonFbsPosting
from apps.marketplaces.ozon_publication import OzonPublicationError, _validated_credentials


class OzonOrderSyncError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _date(value: Any):
    parsed = parse_datetime(str(value or '').strip())
    return parsed if parsed and parsed.tzinfo else None


def _products(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > 100:
        raise OzonOrderSyncError('invalid_response', 'Ozon вернул некорректный состав заказа.')
    result = []
    for item in raw:
        if not isinstance(item, dict):
            raise OzonOrderSyncError('invalid_response', 'Ozon вернул некорректный состав заказа.')
        quantity = item.get('quantity', 0)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            raise OzonOrderSyncError('invalid_response', 'Ozon вернул некорректное количество товара.')
        result.append({
            'offer_id': str(item.get('offer_id') or '')[:100],
            'sku': str(item.get('sku') or '')[:100],
            'name': str(item.get('name') or '')[:500],
            'quantity': quantity,
            'price': str(item.get('price') or '')[:50],
        })
    return result


def sync_fbs_orders(account: MarketplaceAccount, *, days: int = 14) -> int:
    if account.marketplace != MarketplaceAccount.MARKETPLACE_OZON or not account.is_active:
        raise OzonOrderSyncError('account_not_ready', 'Кабинет Ozon не подключён.')
    if days < 1 or days > 30:
        raise OzonOrderSyncError('range_invalid', 'Период должен быть от 1 до 30 дней.')
    try:
        client_id, api_key = _validated_credentials(account)
    except OzonPublicationError as exc:
        raise OzonOrderSyncError(exc.code, str(exc)) from exc
    end = timezone.now()
    start = end - timedelta(days=days)
    client = OzonSellerClient(client_id=client_id, api_key=api_key)
    imported = 0
    for page in range(10):
        try:
            postings, has_next = client.list_fbs_postings(
                since=start.isoformat(), to=end.isoformat(), limit=100, offset=page * 100,
            )
        except OzonAPIError as exc:
            raise OzonOrderSyncError(exc.code, str(exc)) from exc
        now = timezone.now()
        with transaction.atomic():
            for raw in postings:
                number = str(raw.get('posting_number') or '').strip()
                if not number or len(number) > 100:
                    raise OzonOrderSyncError('invalid_response', 'Ozon вернул заказ без номера.')
                warehouse = raw.get('warehouse_id')
                if warehouse is None and isinstance(raw.get('delivery_method'), dict):
                    warehouse = raw['delivery_method'].get('warehouse_id')
                OzonFbsPosting.objects.update_or_create(
                    account=account, posting_number=number,
                    defaults={
                        'tenant': account.tenant,
                        'status': str(raw.get('status') or '')[:100],
                        'substatus': str(raw.get('substatus') or '')[:100],
                        'in_process_at': _date(raw.get('in_process_at')),
                        'shipment_date': _date(raw.get('shipment_date')),
                        'warehouse_id': str(warehouse or '')[:100],
                        'products': _products(raw.get('products')),
                        'provider_updated_at': _date(raw.get('updated_at')),
                        'last_synced_at': now,
                    },
                )
                imported += 1
        if not has_next:
            return imported
    raise OzonOrderSyncError('page_limit_exceeded', 'Заказов больше безопасного лимита синхронизации.')
