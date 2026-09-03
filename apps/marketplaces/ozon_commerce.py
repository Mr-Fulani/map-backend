"""Guarded price and single-warehouse stock synchronization for Ozon."""

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import Any, Callable

from django.db import transaction
from django.utils import timezone

from apps.marketplaces.adapters.ozon.client import OzonAPIError, OzonSellerClient
from apps.marketplaces.models import (
    MarketplaceAccount,
    OzonAccountProfile,
    OzonOfferDraft,
    OzonOperation,
)
from apps.marketplaces.ozon_publication import (
    OzonPublicationError,
    _request_digest,
    _validated_credentials,
    operation_presentation,
)
from apps.marketplaces.ozon_rollout import ozon_product_write_enabled_for_account
from apps.products.models import Product


class OzonCommerceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


PRICE_HOURLY_LIMIT = 10
STOCK_MIN_INTERVAL = timedelta(seconds=30)


def commerce_presentation(
    product: Product,
    account: MarketplaceAccount,
    draft: OzonOfferDraft | None,
    pricing: Mapping[str, Any] | None,
) -> dict[str, Any]:
    price_operation = None
    stock_operation = None
    if draft is not None:
        price_operation = draft.operations.filter(
            kind=OzonOperation.Kind.PRICE_UPDATE,
        ).order_by('-created_at').first()
        stock_operation = draft.operations.filter(
            kind=OzonOperation.Kind.STOCK_UPDATE,
        ).order_by('-created_at').first()
    profile = OzonAccountProfile.objects.filter(account=account).first()
    return {
        'can_sync': bool(
            draft is not None
            and draft.publication_status == 'published'
            and draft.provider_product_id
            and ozon_product_write_enabled_for_account(account)
        ),
        'desired_price': pricing.get('final_price') if pricing is not None else None,
        'desired_stock': product.stock_qty,
        'warehouse_id': profile.selected_warehouse_id if profile is not None else '',
        'warehouse_name': profile.selected_warehouse_name if profile is not None else '',
        'last_synced_price': (
            str(draft.last_synced_price)
            if draft is not None and draft.last_synced_price is not None
            else None
        ),
        'last_price_sync_at': draft.last_price_sync_at if draft is not None else None,
        'last_synced_stock': draft.last_synced_stock if draft is not None else None,
        'last_stock_sync_at': draft.last_stock_sync_at if draft is not None else None,
        'last_stock_warehouse_id': (
            draft.last_stock_warehouse_id if draft is not None else ''
        ),
        'price_operation': operation_presentation(price_operation),
        'stock_operation': operation_presentation(stock_operation),
    }


def _require_commerce_admission(
    account: MarketplaceAccount,
    draft: OzonOfferDraft,
) -> OzonAccountProfile:
    if not ozon_product_write_enabled_for_account(account):
        raise OzonCommerceError(
            'write_disabled',
            'Синхронизация выключена для этого кабинета Ozon.',
        )
    try:
        profile = account.ozon_profile
    except OzonAccountProfile.DoesNotExist as exc:
        raise OzonCommerceError('account_not_ready', 'Сначала подключите кабинет Ozon.') from exc
    if profile.connection_status != OzonAccountProfile.ConnectionStatus.CONNECTED:
        raise OzonCommerceError('account_not_ready', 'Сначала проверьте подключение Ozon.')
    if not profile.selected_warehouse_id:
        raise OzonCommerceError('warehouse_missing', 'Выберите один FBS-склад Ozon.')
    if draft.publication_status != 'published' or not draft.provider_product_id:
        raise OzonCommerceError(
            'offer_not_published',
            'Сначала дождитесь успешной публикации и модерации карточки Ozon.',
        )
    return profile


def _positive_warehouse_id(value: Any) -> int:
    if isinstance(value, bool):
        raise OzonCommerceError('warehouse_invalid', 'Ozon вернул некорректный ID склада.')
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise OzonCommerceError(
            'warehouse_invalid',
            'Ozon вернул некорректный ID склада.',
        ) from exc
    if result <= 0:
        raise OzonCommerceError('warehouse_invalid', 'Ozon вернул некорректный ID склада.')
    return result


def _existing_or_active(
    draft: OzonOfferDraft,
    account: MarketplaceAccount,
    kind: str,
    idempotency_key: str,
) -> OzonOperation | None:
    existing = OzonOperation.objects.filter(
        account=account,
        idempotency_key=idempotency_key,
    ).first()
    if existing is not None:
        if existing.offer_id != draft.pk or existing.kind != kind:
            raise OzonCommerceError(
                'idempotency_conflict',
                'Идентификатор синхронизации уже использован для другой операции.',
            )
        return existing
    blocking = OzonOperation.objects.filter(
        offer=draft,
        kind=kind,
        state__in=(
            OzonOperation.ACTIVE_STATES
            - {OzonOperation.State.OUTCOME_UNKNOWN}
        ),
    ).order_by('-created_at').first()
    if blocking is not None:
        return blocking

    # A repeated request with the same idempotency key still returns the
    # unknown operation above. A new, explicit request may retry these
    # set-value mutations after the provider and local rate guards allow it.
    return OzonOperation.objects.filter(
        offer=draft,
        kind=kind,
        state=OzonOperation.State.OUTCOME_UNKNOWN,
        retry_after_at__gt=timezone.now(),
    ).order_by('-created_at').first()


def _new_operation(
    *,
    draft: OzonOfferDraft,
    kind: str,
    idempotency_key: str,
    item: dict[str, Any],
    summary: dict[str, Any],
) -> OzonOperation:
    return OzonOperation.objects.create(
        tenant=draft.tenant,
        account=draft.account,
        offer=draft,
        kind=kind,
        idempotency_key=idempotency_key,
        request_sha256=_request_digest(item),
        request_summary=summary,
    )


def sync_offer_commerce(
    product: Product,
    account: MarketplaceAccount,
    *,
    idempotency_key: str,
) -> tuple[OzonOperation | None, OzonOperation | None]:
    """Submit at most one price and one stock update for the exact offer."""

    normalized_key = str(idempotency_key).strip()
    if not normalized_key or len(normalized_key) > 90:
        raise OzonCommerceError('idempotency_key_invalid', 'Повторите синхронизацию.')

    from apps.marketplaces.ozon_offers import offer_presentation

    with transaction.atomic():
        locked_account = MarketplaceAccount.objects.select_for_update().select_related(
            'tenant',
        ).get(pk=account.pk, tenant=product.tenant)
        locked_product = Product.objects.select_for_update().get(
            pk=product.pk,
            tenant=product.tenant,
        )
        draft = OzonOfferDraft.objects.select_for_update().filter(
            tenant=product.tenant,
            product=locked_product,
            account=locked_account,
        ).first()
        if draft is None:
            raise OzonCommerceError('draft_missing', 'Сначала подготовьте карточку Ozon.')
        profile = _require_commerce_admission(locked_account, draft)
        presentation = offer_presentation(locked_product, locked_account)
        pricing = presentation.get('pricing')
        if not isinstance(pricing, Mapping):
            raise OzonCommerceError('price_missing', 'Не удалось рассчитать цену Ozon.')
        desired_price = Decimal(str(pricing['final_price'])).quantize(Decimal('0.01'))
        if desired_price <= 0:
            raise OzonCommerceError('price_invalid', 'Цена Ozon должна быть больше нуля.')
        desired_stock = int(locked_product.stock_qty)
        warehouse_id = _positive_warehouse_id(profile.selected_warehouse_id)
        currency = (profile.currency or 'RUB')[:10]

        price_key = f'{normalized_key}:price'
        price_operation = _existing_or_active(
            draft,
            locked_account,
            OzonOperation.Kind.PRICE_UPDATE,
            price_key,
        )
        if price_operation is None and draft.last_synced_price != desired_price:
            hourly_attempts = OzonOperation.objects.filter(
                offer=draft,
                kind=OzonOperation.Kind.PRICE_UPDATE,
                last_attempt_at__gte=timezone.now() - timedelta(hours=1),
            ).count()
            if hourly_attempts >= PRICE_HOURLY_LIMIT:
                raise OzonCommerceError(
                    'price_hourly_limit',
                    'Цена этого товара уже менялась 10 раз за час. Повторите позже.',
                )
            price_item = {
                'auto_action_enabled': 'UNKNOWN',
                'currency_code': currency,
                'min_price': '0',
                'offer_id': draft.offer_id,
                'old_price': '0',
                'price': str(desired_price),
                'product_id': draft.provider_product_id,
            }
            price_operation = _new_operation(
                draft=draft,
                kind=OzonOperation.Kind.PRICE_UPDATE,
                idempotency_key=price_key,
                item=price_item,
                summary={
                    'offer_id': draft.offer_id,
                    'product_id': draft.provider_product_id,
                    'price': str(desired_price),
                    'currency_code': currency,
                },
            )

        stock_key = f'{normalized_key}:stock'
        stock_operation = _existing_or_active(
            draft,
            locked_account,
            OzonOperation.Kind.STOCK_UPDATE,
            stock_key,
        )
        stock_is_current = (
            draft.last_synced_stock == desired_stock
            and draft.last_stock_warehouse_id == str(warehouse_id)
        )
        if stock_operation is None and not stock_is_current:
            cooldown = OzonOperation.objects.filter(
                offer=draft,
                kind=OzonOperation.Kind.STOCK_UPDATE,
                last_attempt_at__gte=timezone.now() - STOCK_MIN_INTERVAL,
            ).exists()
            if cooldown:
                raise OzonCommerceError(
                    'stock_cooldown',
                    'Остаток этого товара уже отправлялся менее 30 секунд назад.',
                )
            stock_item = {
                'offer_id': draft.offer_id,
                'product_id': draft.provider_product_id,
                'stock': desired_stock,
                'warehouse_id': warehouse_id,
            }
            stock_operation = _new_operation(
                draft=draft,
                kind=OzonOperation.Kind.STOCK_UPDATE,
                idempotency_key=stock_key,
                item=stock_item,
                summary={
                    'offer_id': draft.offer_id,
                    'product_id': draft.provider_product_id,
                    'stock': desired_stock,
                    'warehouse_id': warehouse_id,
                },
            )

    if price_operation is not None:
        price_operation = _submit_commerce_operation(price_operation.pk)
    if stock_operation is not None:
        stock_operation = _submit_commerce_operation(stock_operation.pk)
    return price_operation, stock_operation


def _provider_errors(raw: Any, *, code: str, fallback: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raw = []
    errors = []
    for item in raw[:20]:
        if not isinstance(item, Mapping):
            continue
        message = str(item.get('message') or item.get('description') or fallback).strip()[:700]
        errors.append({
            'code': code,
            'provider_code': str(item.get('code') or '').strip()[:100],
            'message': message or fallback,
        })
    return errors or [{'code': code, 'message': fallback}]


def _mark_submission_error(
    operation_id,
    exc: OzonAPIError,
) -> OzonOperation:
    with transaction.atomic():
        operation = OzonOperation.objects.select_for_update().get(pk=operation_id)
        unknown = exc.code in {'connection_error', 'provider_unavailable'}
        operation.state = (
            OzonOperation.State.OUTCOME_UNKNOWN if unknown else OzonOperation.State.FAILED
        )
        operation.errors = [{'code': exc.code, 'message': str(exc)}]
        operation.retry_after_at = (
            timezone.now() + timedelta(seconds=exc.retry_after_seconds)
            if exc.retry_after_seconds is not None else None
        )
        operation.completed_at = None if unknown else timezone.now()
        operation.save(update_fields=[
            'state', 'errors', 'retry_after_at', 'completed_at', 'updated_at',
        ])
        return operation


def _submit_commerce_operation(operation_id) -> OzonOperation:
    with transaction.atomic():
        operation = OzonOperation.objects.select_for_update().select_related(
            'account', 'offer',
        ).get(pk=operation_id)
        if operation.state != OzonOperation.State.QUEUED:
            return operation
        try:
            client_id, api_key = _validated_credentials(operation.account)
        except OzonPublicationError as exc:
            operation.state = OzonOperation.State.FAILED
            operation.errors = [{'code': exc.code, 'message': str(exc)}]
            operation.completed_at = timezone.now()
            operation.save(update_fields=['state', 'errors', 'completed_at', 'updated_at'])
            return operation
        operation.state = OzonOperation.State.SENDING
        operation.attempt_count += 1
        operation.last_attempt_at = timezone.now()
        operation.save(update_fields=['state', 'attempt_count', 'last_attempt_at', 'updated_at'])
        summary = dict(operation.request_summary)

    client = OzonSellerClient(client_id=client_id, api_key=api_key)
    if operation.kind == OzonOperation.Kind.PRICE_UPDATE:
        item = {
            'auto_action_enabled': 'UNKNOWN',
            'currency_code': summary['currency_code'],
            'min_price': '0',
            'offer_id': summary['offer_id'],
            'old_price': '0',
            'price': summary['price'],
            'product_id': summary['product_id'],
        }
        provider_call: Callable = client.update_prices
        payload = [item]
        error_code = 'price_rejected'
        fallback = 'Ozon отклонил обновление цены.'
    else:
        item = {
            'offer_id': summary['offer_id'],
            'product_id': summary['product_id'],
            'stock': summary['stock'],
            'warehouse_id': summary['warehouse_id'],
        }
        provider_call = client.update_stocks
        payload = [item]
        error_code = 'stock_rejected'
        fallback = 'Ozon отклонил обновление остатка.'
    try:
        results = provider_call(payload)
    except OzonAPIError as exc:
        return _mark_submission_error(operation_id, exc)

    exact = next((
        result for result in results
        if str(result.get('offer_id') or '') == str(summary['offer_id'])
        and (
            operation.kind != OzonOperation.Kind.STOCK_UPDATE
            or str(result.get('warehouse_id') or '') == str(summary['warehouse_id'])
        )
    ), None)
    if exact is None:
        with transaction.atomic():
            operation = OzonOperation.objects.select_for_update().get(pk=operation_id)
            operation.state = OzonOperation.State.MANUAL_REVIEW
            operation.errors = [{
                'code': 'result_not_matched',
                'message': 'Ozon не вернул точный результат для отправленного товара.',
            }]
            operation.completed_at = timezone.now()
            operation.save(update_fields=['state', 'errors', 'completed_at', 'updated_at'])
            return operation

    raw_errors = exact.get('errors')
    if isinstance(raw_errors, list) and raw_errors:
        with transaction.atomic():
            operation = OzonOperation.objects.select_for_update().get(pk=operation_id)
            operation.state = OzonOperation.State.FAILED
            operation.errors = _provider_errors(raw_errors, code=error_code, fallback=fallback)
            operation.response_summary = {'updated': False}
            operation.completed_at = timezone.now()
            operation.save(update_fields=[
                'state', 'errors', 'response_summary', 'completed_at', 'updated_at',
            ])
            return operation
    if exact.get('updated') is not True:
        with transaction.atomic():
            operation = OzonOperation.objects.select_for_update().get(pk=operation_id)
            operation.state = OzonOperation.State.MANUAL_REVIEW
            operation.errors = [{
                'code': 'update_not_confirmed',
                'message': 'Ozon не подтвердил применение нового значения.',
            }]
            operation.response_summary = {'updated': False}
            operation.completed_at = timezone.now()
            operation.save(update_fields=[
                'state', 'errors', 'response_summary', 'completed_at', 'updated_at',
            ])
            return operation

    with transaction.atomic():
        operation = OzonOperation.objects.select_for_update().select_related('offer').get(
            pk=operation_id,
        )
        operation.state = OzonOperation.State.SUCCEEDED
        operation.errors = []
        operation.response_summary = {'updated': True}
        operation.completed_at = timezone.now()
        operation.save(update_fields=[
            'state', 'errors', 'response_summary', 'completed_at', 'updated_at',
        ])
        draft = operation.offer
        if operation.kind == OzonOperation.Kind.PRICE_UPDATE:
            draft.last_synced_price = Decimal(summary['price'])
            draft.last_price_sync_at = timezone.now()
            draft.save(update_fields=[
                'last_synced_price', 'last_price_sync_at', 'updated_at',
            ])
        else:
            draft.last_synced_stock = int(summary['stock'])
            draft.last_stock_warehouse_id = str(summary['warehouse_id'])
            draft.last_stock_sync_at = timezone.now()
            draft.save(update_fields=[
                'last_synced_stock', 'last_stock_warehouse_id',
                'last_stock_sync_at', 'updated_at',
            ])
        return operation
