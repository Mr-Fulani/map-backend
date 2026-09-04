"""Safe, durable lifecycle mutations for one account-scoped Ozon offer."""

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
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
)
from apps.marketplaces.ozon_rollout import ozon_product_archive_enabled_for_account
from apps.products.models import Product


class OzonLifecycleError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


ARCHIVE_RECONCILE_LIMIT = 6
ARCHIVE_RECONCILE_DELAY = timedelta(seconds=30)


def _positive_id(value: Any, *, code: str, message: str) -> int:
    if isinstance(value, bool):
        raise OzonLifecycleError(code, message)
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise OzonLifecycleError(code, message) from exc
    if result <= 0:
        raise OzonLifecycleError(code, message)
    return result


def _require_archive_admission(
    account: MarketplaceAccount,
    draft: OzonOfferDraft,
) -> tuple[OzonAccountProfile, int, int]:
    if not ozon_product_archive_enabled_for_account(account):
        raise OzonLifecycleError(
            'archive_disabled',
            'У API-ключа Ozon нет права на архивирование товаров.',
        )
    try:
        profile = account.ozon_profile
    except OzonAccountProfile.DoesNotExist as exc:
        raise OzonLifecycleError(
            'account_not_ready', 'Сначала проверьте подключение Ozon.',
        ) from exc
    if profile.connection_status != OzonAccountProfile.ConnectionStatus.CONNECTED:
        raise OzonLifecycleError(
            'account_not_ready', 'Сначала проверьте подключение Ozon.',
        )
    product_id = _positive_id(
        draft.provider_product_id,
        code='provider_product_missing',
        message='Сначала дождитесь, пока Ozon присвоит товару ID.',
    )
    warehouse_id = _positive_id(
        profile.selected_warehouse_id,
        code='warehouse_missing',
        message='Выберите один FBS-склад Ozon.',
    )
    if draft.publication_status == 'archived':
        raise OzonLifecycleError('already_archived', 'Товар уже снят с публикации Ozon.')
    if draft.publication_status not in {
        'published',
        'archive_failed',
        'archive_outcome_unknown',
    }:
        raise OzonLifecycleError(
            'offer_not_published',
            'Снять можно только уже опубликованную карточку Ozon.',
        )
    return profile, product_id, warehouse_id


def _operation_error(
    operation_id,
    *,
    code: str,
    message: str,
    state: str = OzonOperation.State.FAILED,
    response_summary: Mapping[str, Any] | None = None,
) -> OzonOperation:
    with transaction.atomic():
        operation = OzonOperation.objects.select_for_update().select_related('offer').get(
            pk=operation_id,
        )
        operation.state = state
        operation.errors = [{'code': code, 'message': message}]
        operation.response_summary = {
            **operation.response_summary,
            **dict(response_summary or {}),
        }
        operation.completed_at = (
            None if state in OzonOperation.ACTIVE_STATES else timezone.now()
        )
        operation.next_reconcile_at = (
            timezone.now() + ARCHIVE_RECONCILE_DELAY
            if state in {OzonOperation.State.RECONCILING, OzonOperation.State.OUTCOME_UNKNOWN}
            else None
        )
        operation.save(update_fields=[
            'state', 'errors', 'response_summary', 'completed_at',
            'next_reconcile_at', 'updated_at',
        ])
        draft = operation.offer
        draft.publication_status = (
            'archive_outcome_unknown'
            if state in OzonOperation.ACTIVE_STATES
            else 'archive_failed'
        )
        draft.provider_errors = operation.errors
        draft.last_provider_sync_at = timezone.now()
        draft.save(update_fields=[
            'publication_status', 'provider_errors',
            'last_provider_sync_at', 'updated_at',
        ])
        return operation


def _stock_zero_confirmed(result: Mapping[str, Any], summary: Mapping[str, Any]) -> bool:
    return (
        str(result.get('offer_id') or '') == str(summary['offer_id'])
        and str(result.get('warehouse_id') or '') == str(summary['warehouse_id'])
        and not result.get('errors')
        and result.get('updated') is True
    )


def _persist_archive_projection(
    operation_id,
    product_item: Mapping[str, Any] | None,
) -> OzonOperation:
    with transaction.atomic():
        operation = OzonOperation.objects.select_for_update().select_related('offer').get(
            pk=operation_id,
        )
        draft = operation.offer
        archived = product_item.get('is_archived') if product_item is not None else None
        operation.last_reconciled_at = timezone.now()
        operation.response_summary = {
            **operation.response_summary,
            'archive_observed': archived is True,
        }
        draft.last_provider_sync_at = timezone.now()
        if archived is True:
            operation.state = OzonOperation.State.SUCCEEDED
            operation.errors = []
            operation.completed_at = timezone.now()
            operation.next_reconcile_at = None
            draft.publication_status = 'archived'
            draft.provider_status = 'archived'
            draft.provider_errors = []
        elif operation.reconcile_count < ARCHIVE_RECONCILE_LIMIT:
            operation.state = OzonOperation.State.RECONCILING
            operation.errors = []
            operation.next_reconcile_at = timezone.now() + ARCHIVE_RECONCILE_DELAY
            draft.publication_status = 'archive_processing'
            draft.provider_errors = []
        else:
            operation.state = OzonOperation.State.MANUAL_REVIEW
            operation.errors = [{
                'code': 'archive_not_confirmed',
                'message': (
                    'Ozon не подтвердил архивный статус после '
                    'нескольких проверок. Товар оставлен с нулевым остатком.'
                ),
            }]
            operation.completed_at = timezone.now()
            operation.next_reconcile_at = None
            draft.publication_status = 'archive_failed'
            draft.provider_errors = operation.errors
        operation.save(update_fields=[
            'state', 'errors', 'completed_at', 'next_reconcile_at',
            'last_reconciled_at', 'response_summary', 'updated_at',
        ])
        draft.save(update_fields=[
            'publication_status', 'provider_status', 'provider_errors',
            'last_provider_sync_at', 'updated_at',
        ])
        return operation


def _read_archive_projection(
    operation: OzonOperation,
    client: OzonSellerClient,
) -> OzonOperation:
    try:
        product_item = client.get_product_info_by_offer_id(operation.offer.offer_id)
    except OzonAPIError as exc:
        return _operation_error(
            operation.pk,
            code='archive_status_unavailable',
            message=str(exc),
            state=OzonOperation.State.RECONCILING,
        )
    return _persist_archive_projection(operation.pk, product_item)


def request_product_archive(
    product: Product,
    account: MarketplaceAccount,
    *,
    idempotency_key: str,
) -> OzonOperation:
    """Zero the exact FBS stock, archive the product, then verify provider state."""

    normalized_key = str(idempotency_key).strip()
    if not normalized_key or len(normalized_key) > 100:
        raise OzonLifecycleError('idempotency_key_invalid', 'Повторите снятие с публикации.')

    with transaction.atomic():
        locked_account = MarketplaceAccount.objects.select_for_update().select_related(
            'tenant',
        ).get(pk=account.pk, tenant=product.tenant)
        locked_product = Product.objects.select_for_update().get(
            pk=product.pk, tenant=product.tenant,
        )
        draft = OzonOfferDraft.objects.select_for_update().filter(
            tenant=product.tenant,
            product=locked_product,
            account=locked_account,
        ).first()
        if draft is None:
            raise OzonLifecycleError('draft_missing', 'Карточка Ozon не подготовлена.')
        existing = OzonOperation.objects.filter(
            account=locked_account,
            idempotency_key=normalized_key,
        ).first()
        if existing is not None:
            if existing.offer_id != draft.pk or existing.kind != OzonOperation.Kind.ARCHIVE:
                raise OzonLifecycleError(
                    'idempotency_conflict',
                    'Идентификатор уже использован для другой операции.',
                )
            return existing
        active = OzonOperation.objects.filter(
            offer=draft,
            kind=OzonOperation.Kind.ARCHIVE,
            state__in=OzonOperation.ACTIVE_STATES,
        ).order_by('-created_at').first()
        if active is not None:
            return active
        profile, provider_product_id, warehouse_id = _require_archive_admission(
            locked_account, draft,
        )
        request_item = {
            'offer_id': draft.offer_id,
            'product_id': provider_product_id,
            'warehouse_id': warehouse_id,
            'stock': 0,
        }
        try:
            with transaction.atomic():
                operation = OzonOperation.objects.create(
                    tenant=product.tenant,
                    account=locked_account,
                    offer=draft,
                    kind=OzonOperation.Kind.ARCHIVE,
                    idempotency_key=normalized_key,
                    request_sha256=_request_digest(request_item),
                    request_summary=request_item,
                    response_summary={'stage': 'queued'},
                )
        except IntegrityError:
            raced = OzonOperation.objects.get(
                account=locked_account,
                idempotency_key=normalized_key,
            )
            if raced.offer_id != draft.pk or raced.kind != OzonOperation.Kind.ARCHIVE:
                raise OzonLifecycleError(
                    'idempotency_conflict',
                    'Идентификатор уже использован для другой операции.',
                )
            return raced
        draft.publication_status = 'archive_pending'
        draft.provider_errors = []
        draft.save(update_fields=['publication_status', 'provider_errors', 'updated_at'])

    return _submit_product_archive(operation.pk)


def _submit_product_archive(operation_id) -> OzonOperation:
    with transaction.atomic():
        operation = OzonOperation.objects.select_for_update().select_related(
            'account', 'offer',
        ).get(pk=operation_id)
        if operation.state != OzonOperation.State.QUEUED:
            return operation
        try:
            client_id, api_key = _validated_credentials(operation.account)
        except OzonPublicationError as exc:
            return _operation_error(operation.pk, code=exc.code, message=str(exc))
        operation.state = OzonOperation.State.SENDING
        operation.attempt_count += 1
        operation.last_attempt_at = timezone.now()
        operation.response_summary = {'stage': 'zeroing_stock'}
        operation.save(update_fields=[
            'state', 'attempt_count', 'last_attempt_at',
            'response_summary', 'updated_at',
        ])
        summary = dict(operation.request_summary)

    client = OzonSellerClient(client_id=client_id, api_key=api_key)
    stock_item = {
        'offer_id': summary['offer_id'],
        'product_id': summary['product_id'],
        'stock': 0,
        'warehouse_id': summary['warehouse_id'],
    }
    try:
        stock_results = client.update_stocks([stock_item])
    except OzonAPIError as exc:
        return _operation_error(
            operation_id,
            code='stock_zero_not_confirmed',
            message=(
                'Не удалось подтвердить нулевой остаток. '
                'Архивирование не запущено; повторите действие.'
            ),
            state=(
                OzonOperation.State.MANUAL_REVIEW
                if exc.code in {'connection_error', 'provider_unavailable'}
                else OzonOperation.State.FAILED
            ),
            response_summary={'stage': 'stock_zero_failed'},
        )
    exact_stock = next((
        item for item in stock_results
        if isinstance(item, Mapping) and _stock_zero_confirmed(item, summary)
    ), None)
    if exact_stock is None:
        return _operation_error(
            operation_id,
            code='stock_zero_not_confirmed',
            message=(
                'Ozon не подтвердил обнуление остатка. '
                'Архивирование не запущено.'
            ),
            state=OzonOperation.State.MANUAL_REVIEW,
            response_summary={'stage': 'stock_zero_failed'},
        )

    with transaction.atomic():
        locked = OzonOperation.objects.select_for_update().select_related('offer').get(
            pk=operation_id,
        )
        locked.response_summary = {'stage': 'stock_zeroed'}
        locked.save(update_fields=['response_summary', 'updated_at'])
        draft = locked.offer
        draft.last_synced_stock = 0
        draft.last_stock_warehouse_id = str(summary['warehouse_id'])
        draft.last_stock_sync_at = timezone.now()
        draft.save(update_fields=[
            'last_synced_stock', 'last_stock_warehouse_id',
            'last_stock_sync_at', 'updated_at',
        ])

    try:
        archived = client.archive_products([int(summary['product_id'])])
    except OzonAPIError as exc:
        if exc.code not in {'connection_error', 'provider_unavailable'}:
            return _operation_error(
                operation_id,
                code=exc.code,
                message=str(exc),
                response_summary={'stage': 'archive_failed'},
            )
        with transaction.atomic():
            locked = OzonOperation.objects.select_for_update().get(pk=operation_id)
            locked.state = OzonOperation.State.OUTCOME_UNKNOWN
            locked.errors = [{
                'code': 'archive_outcome_unknown',
                'message': 'Ozon мог принять архивирование. MAP сверит статус.',
            }]
            locked.response_summary = {'stage': 'archive_outcome_unknown'}
            locked.next_reconcile_at = timezone.now()
            locked.save(update_fields=[
                'state', 'errors', 'response_summary', 'next_reconcile_at', 'updated_at',
            ])
            locked.offer.publication_status = 'archive_outcome_unknown'
            locked.offer.provider_errors = locked.errors
            locked.offer.save(update_fields=[
                'publication_status', 'provider_errors', 'updated_at',
            ])
        return _read_archive_projection(locked, client)
    if archived is not True:
        return _operation_error(
            operation_id,
            code='archive_rejected',
            message='Ozon не подтвердил перенос товара в архив.',
            state=OzonOperation.State.MANUAL_REVIEW,
            response_summary={'stage': 'archive_rejected'},
        )

    with transaction.atomic():
        locked = OzonOperation.objects.select_for_update().get(pk=operation_id)
        locked.state = OzonOperation.State.RECONCILING
        locked.errors = []
        locked.response_summary = {'stage': 'archive_accepted'}
        locked.next_reconcile_at = timezone.now()
        locked.save(update_fields=[
            'state', 'errors', 'response_summary', 'next_reconcile_at', 'updated_at',
        ])
        locked.offer.publication_status = 'archive_processing'
        locked.offer.provider_errors = []
        locked.offer.save(update_fields=[
            'publication_status', 'provider_errors', 'updated_at',
        ])
    return _read_archive_projection(locked, client)


def reconcile_product_archive(
    product: Product,
    account: MarketplaceAccount,
) -> OzonOperation:
    """Read and persist the archive state without repeating a mutation."""

    with transaction.atomic():
        locked_account = MarketplaceAccount.objects.select_for_update().select_related(
            'tenant',
        ).get(pk=account.pk, tenant=product.tenant)
        draft = OzonOfferDraft.objects.select_for_update().filter(
            tenant=product.tenant,
            product=product,
            account=locked_account,
        ).first()
        if draft is None:
            raise OzonLifecycleError('draft_missing', 'Карточка Ozon не подготовлена.')
        operation = OzonOperation.objects.select_for_update().filter(
            offer=draft,
            kind=OzonOperation.Kind.ARCHIVE,
        ).order_by('-created_at').first()
        if operation is None:
            raise OzonLifecycleError(
                'archive_operation_missing', 'Снятие с публикации ещё не запускалось.',
            )
        if operation.state not in OzonOperation.ACTIVE_STATES:
            return operation
        if operation.next_reconcile_at and operation.next_reconcile_at > timezone.now():
            return operation
        if operation.reconcile_count >= ARCHIVE_RECONCILE_LIMIT:
            return _persist_archive_projection(operation.pk, None)
        operation.reconcile_count += 1
        operation.last_reconciled_at = timezone.now()
        operation.next_reconcile_at = timezone.now() + ARCHIVE_RECONCILE_DELAY
        operation.save(update_fields=[
            'reconcile_count', 'last_reconciled_at', 'next_reconcile_at', 'updated_at',
        ])
        try:
            client_id, api_key = _validated_credentials(locked_account)
        except OzonPublicationError as exc:
            raise OzonLifecycleError(exc.code, str(exc)) from exc

    client = OzonSellerClient(client_id=client_id, api_key=api_key)
    return _read_archive_projection(operation, client)
