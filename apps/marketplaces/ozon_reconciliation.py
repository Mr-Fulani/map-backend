"""Read-only reconciliation of Ozon product import and moderation state."""

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

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
    _validated_credentials,
)
from apps.marketplaces.ozon_rollout import ozon_connection_enabled_for_account
from apps.products.models import Product


class OzonReconciliationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


RECONCILE_LEASE = timedelta(seconds=15)
UNKNOWN_NEGATIVE_HORIZON = timedelta(minutes=2)
UNKNOWN_NEGATIVE_THRESHOLD = 3
UNKNOWN_SCHEMA_THRESHOLD = 12
MAX_PROVIDER_ERRORS = 20

IMPORT_PENDING = frozenset({'pending', 'queued', 'processing', 'in_progress'})
IMPORT_SUCCEEDED = frozenset({'imported', 'success', 'succeeded', 'completed'})
IMPORT_FAILED = frozenset({'failed', 'error', 'rejected', 'declined'})
MODERATION_APPROVED = frozenset({'approved', 'success', 'succeeded'})
MODERATION_PENDING = frozenset({
    'pending',
    'processing',
    'in_moderation',
    'moderating',
    'not_moderated',
})
MODERATION_REJECTED = frozenset({
    'declined',
    'rejected',
    'failed',
    'error',
    'disabled',
})


def _safe_text(value: Any, *, limit: int = 500) -> str:
    text = ''.join(character for character in str(value or '') if character.isprintable())
    return ' '.join(text.split())[:limit]


def _normalized_status(value: Any) -> str:
    return _safe_text(value, limit=100).casefold().replace('-', '_').replace(' ', '_')


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _friendly_provider_errors(raw: Any, *, code: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raw = []
    errors: list[dict[str, Any]] = []
    for item in raw[:MAX_PROVIDER_ERRORS]:
        if not isinstance(item, Mapping):
            continue
        provider_code = _safe_text(
            item.get('code') or item.get('error_code') or item.get('state'),
            limit=100,
        )
        field = _safe_text(item.get('field') or item.get('field_name'), limit=200)
        attribute_id = _positive_int(item.get('attribute_id'))
        description = _safe_text(
            item.get('description') or item.get('message') or item.get('text'),
            limit=700,
        )
        lowered = f'{provider_code} {description}'.casefold()
        if 'required' in lowered or 'обязат' in lowered:
            message = 'Заполните обязательное поле или характеристику Ozon.'
        elif 'dictionary' in lowered or 'справочник' in lowered:
            message = 'Выберите актуальное значение из справочника Ozon.'
        elif 'image' in lowered or 'изображ' in lowered:
            message = 'Проверьте фотографии и требования Ozon к изображениям.'
        elif 'barcode' in lowered or 'штрих' in lowered:
            message = 'Проверьте штрихкод товара.'
        else:
            message = description or 'Ozon отклонил данные карточки.'
        errors.append({
            'code': code,
            'provider_code': provider_code,
            'field': field,
            'attribute_id': attribute_id,
            'message': message,
        })
    if not errors:
        errors.append({
            'code': code,
            'provider_code': '',
            'field': '',
            'attribute_id': None,
            'message': 'Ozon отклонил данные карточки без детализации.',
        })
    return errors


def _require_read_admission(account: MarketplaceAccount) -> None:
    if (
        account.marketplace != MarketplaceAccount.MARKETPLACE_OZON
        or not account.is_active
        or not ozon_connection_enabled_for_account(account.tenant, account.external_id)
    ):
        raise OzonReconciliationError(
            'status_read_disabled',
            'Проверка статуса закрыта для этого кабинета Ozon.',
        )
    try:
        profile = account.ozon_profile
    except OzonAccountProfile.DoesNotExist as exc:
        raise OzonReconciliationError(
            'account_not_ready',
            'Сначала проверьте подключение Ozon.',
        ) from exc
    if profile.connection_status != OzonAccountProfile.ConnectionStatus.CONNECTED:
        raise OzonReconciliationError(
            'account_not_ready',
            'Сначала проверьте подключение Ozon.',
        )


def _claim_reconciliation(
    product: Product,
    account: MarketplaceAccount,
) -> tuple[OzonOperation, bool, str, str]:
    now = timezone.now()
    with transaction.atomic():
        locked_account = MarketplaceAccount.objects.select_for_update().select_related(
            'tenant',
        ).get(pk=account.pk, tenant=product.tenant)
        _require_read_admission(locked_account)
        draft = OzonOfferDraft.objects.select_for_update().filter(
            tenant=product.tenant,
            product=product,
            account=locked_account,
        ).first()
        if draft is None:
            raise OzonReconciliationError(
                'draft_missing',
                'Сначала подготовьте карточку Ozon.',
            )
        operation = OzonOperation.objects.select_for_update().filter(
            offer=draft,
            kind=OzonOperation.Kind.PRODUCT_IMPORT,
        ).order_by('-created_at').first()
        if operation is None:
            raise OzonReconciliationError(
                'operation_missing',
                'Карточка ещё не отправлялась в Ozon.',
            )
        if operation.state not in OzonOperation.ACTIVE_STATES:
            return operation, False, '', ''
        if operation.next_reconcile_at and operation.next_reconcile_at > now:
            return operation, False, '', ''
        if operation.reconcile_count >= 100:
            operation.state = OzonOperation.State.MANUAL_REVIEW
            operation.errors = [{
                'code': 'reconcile_limit',
                'message': 'Достигнут безопасный лимит проверок Ozon.',
            }]
            operation.completed_at = now
            operation.next_reconcile_at = None
            operation.save(update_fields=[
                'state', 'errors', 'completed_at', 'next_reconcile_at', 'updated_at',
            ])
            draft.publication_status = 'manual_review'
            draft.provider_errors = operation.errors
            draft.save(update_fields=['publication_status', 'provider_errors', 'updated_at'])
            return operation, False, '', ''
        operation.reconcile_count += 1
        operation.last_reconciled_at = now
        operation.next_reconcile_at = now + RECONCILE_LEASE
        operation.save(update_fields=[
            'reconcile_count', 'last_reconciled_at', 'next_reconcile_at', 'updated_at',
        ])
        client_id, api_key = _validated_credentials(locked_account)
        return operation, True, client_id, api_key


def _persist_transient_error(operation_id, exc: OzonAPIError) -> OzonOperation:
    with transaction.atomic():
        operation = OzonOperation.objects.select_for_update().select_related('offer').get(
            pk=operation_id,
        )
        now = timezone.now()
        delay = exc.retry_after_seconds if exc.retry_after_seconds is not None else 30
        operation.next_reconcile_at = now + timedelta(seconds=max(delay, 15))
        operation.response_summary = {
            **operation.response_summary,
            'last_status_error': {'code': exc.code, 'message': str(exc)},
        }
        operation.save(update_fields=['next_reconcile_at', 'response_summary', 'updated_at'])
        operation.offer.provider_errors = [{
            'code': 'status_check_failed',
            'message': str(exc),
        }]
        operation.offer.save(update_fields=['provider_errors', 'updated_at'])
        return operation


def _finish_failed(
    operation_id,
    *,
    publication_status: str,
    errors: list[dict[str, Any]],
    state: str = OzonOperation.State.FAILED,
    provider_status: str = '',
    moderation_status: str = '',
    response_summary: Mapping[str, Any] | None = None,
) -> OzonOperation:
    with transaction.atomic():
        operation = OzonOperation.objects.select_for_update().select_related('offer').get(
            pk=operation_id,
        )
        operation.state = state
        operation.errors = errors
        operation.completed_at = timezone.now()
        operation.next_reconcile_at = None
        if response_summary is not None:
            operation.response_summary = dict(response_summary)
        operation.save(update_fields=[
            'state', 'errors', 'completed_at', 'next_reconcile_at',
            'response_summary', 'updated_at',
        ])
        draft = operation.offer
        draft.publication_status = publication_status
        draft.provider_status = provider_status
        draft.moderation_status = moderation_status
        draft.provider_errors = errors
        draft.last_provider_sync_at = timezone.now()
        draft.save(update_fields=[
            'publication_status', 'provider_status', 'moderation_status',
            'provider_errors', 'last_provider_sync_at', 'updated_at',
        ])
        return operation


def _persist_product_projection(
    operation_id,
    item: Mapping[str, Any],
) -> OzonOperation:
    raw_statuses = item.get('statuses')
    statuses: Mapping[str, Any] = (
        raw_statuses if isinstance(raw_statuses, Mapping) else {}
    )
    provider_status = _safe_text(
        statuses.get('status') or statuses.get('status_name'),
        limit=100,
    )
    moderation_status = _safe_text(statuses.get('moderate_status'), limit=100)
    validation_status = _normalized_status(statuses.get('validation_status'))
    normalized_provider = _normalized_status(provider_status)
    normalized_moderation = _normalized_status(moderation_status)
    raw_errors = item.get('errors')
    has_errors = isinstance(raw_errors, list) and bool(raw_errors)
    rejected = (
        has_errors
        or normalized_moderation in MODERATION_REJECTED
        or validation_status in MODERATION_REJECTED
        or normalized_provider in MODERATION_REJECTED
    )
    if rejected:
        return _finish_failed(
            operation_id,
            publication_status='moderation_failed',
            errors=_friendly_provider_errors(raw_errors, code='moderation_rejected'),
            provider_status=provider_status,
            moderation_status=moderation_status,
            response_summary={'product_statuses': dict(statuses)},
        )

    approved = normalized_moderation in MODERATION_APPROVED
    pending = (
        normalized_moderation in MODERATION_PENDING
        or normalized_provider in MODERATION_PENDING
        or validation_status in MODERATION_PENDING
    )
    with transaction.atomic():
        operation = OzonOperation.objects.select_for_update().select_related('offer').get(
            pk=operation_id,
        )
        draft = operation.offer
        draft.provider_product_id = _positive_int(item.get('id') or item.get('product_id'))
        draft.provider_sku = _positive_int(
            item.get('sku') or item.get('sku_fbs') or item.get('sku_fbo'),
        )
        draft.provider_status = provider_status
        draft.moderation_status = moderation_status
        draft.provider_errors = []
        draft.last_provider_sync_at = timezone.now()
        operation.response_summary = {
            **operation.response_summary,
            'product_statuses': dict(statuses),
        }
        if approved:
            operation.state = OzonOperation.State.SUCCEEDED
            operation.completed_at = timezone.now()
            operation.next_reconcile_at = None
            draft.publication_status = 'published'
        elif pending or operation.reconcile_count < UNKNOWN_SCHEMA_THRESHOLD:
            operation.state = OzonOperation.State.RECONCILING
            operation.next_reconcile_at = timezone.now() + timedelta(seconds=30)
            draft.publication_status = 'moderation_pending'
        else:
            operation.state = OzonOperation.State.MANUAL_REVIEW
            operation.errors = [{
                'code': 'unknown_moderation_status',
                'message': 'Ozon вернул новый или неизвестный статус модерации.',
            }]
            operation.completed_at = timezone.now()
            operation.next_reconcile_at = None
            draft.publication_status = 'manual_review'
            draft.provider_errors = operation.errors
        operation.save(update_fields=[
            'state', 'errors', 'completed_at', 'next_reconcile_at',
            'response_summary', 'updated_at',
        ])
        draft.save(update_fields=[
            'provider_product_id', 'provider_sku', 'provider_status',
            'moderation_status', 'provider_errors', 'last_provider_sync_at',
            'publication_status', 'updated_at',
        ])
        return operation


def _persist_not_found(operation_id) -> OzonOperation:
    with transaction.atomic():
        operation = OzonOperation.objects.select_for_update().select_related('offer').get(
            pk=operation_id,
        )
        old_enough = timezone.now() - operation.created_at >= UNKNOWN_NEGATIVE_HORIZON
        if (
            operation.state == OzonOperation.State.OUTCOME_UNKNOWN
            and operation.reconcile_count >= UNKNOWN_NEGATIVE_THRESHOLD
            and old_enough
        ):
            operation.state = OzonOperation.State.FAILED
            operation.errors = [{
                'code': 'provider_not_found',
                'message': 'После нескольких проверок Ozon не нашёл эту версию карточки.',
            }]
            operation.completed_at = timezone.now()
            operation.next_reconcile_at = None
            operation.offer.publication_status = 'not_accepted'
            operation.offer.provider_errors = operation.errors
        else:
            operation.next_reconcile_at = timezone.now() + timedelta(seconds=30)
        operation.response_summary = {
            **operation.response_summary,
            'offer_observed': False,
        }
        operation.save(update_fields=[
            'state', 'errors', 'completed_at', 'next_reconcile_at',
            'response_summary', 'updated_at',
        ])
        operation.offer.last_provider_sync_at = timezone.now()
        operation.offer.save(update_fields=[
            'publication_status', 'provider_errors', 'last_provider_sync_at', 'updated_at',
        ])
        return operation


def reconcile_product_import(
    product: Product,
    account: MarketplaceAccount,
) -> OzonOperation:
    """Perform at most one task-status read and one exact-offer read."""

    try:
        operation, claimed, client_id, api_key = _claim_reconciliation(product, account)
    except OzonPublicationError as exc:
        raise OzonReconciliationError(exc.code, str(exc)) from exc
    if not claimed:
        return operation
    client = OzonSellerClient(client_id=client_id, api_key=api_key)

    if operation.provider_task_id:
        try:
            result = client.get_product_import_info(operation.provider_task_id)
        except OzonAPIError as exc:
            return _persist_transient_error(operation.pk, exc)
        raw_items = result.get('items')
        task_items = raw_items if isinstance(raw_items, list) else []
        item = next((
            candidate
            for candidate in task_items
            if isinstance(candidate, Mapping)
            and str(candidate.get('offer_id') or '').strip() == operation.offer.offer_id
        ), None)
        if item is None:
            with transaction.atomic():
                locked = OzonOperation.objects.select_for_update().get(pk=operation.pk)
                locked.response_summary = {
                    **locked.response_summary,
                    'task_status': 'offer_not_in_task_result',
                }
                locked.next_reconcile_at = timezone.now() + timedelta(seconds=30)
                locked.save(update_fields=[
                    'response_summary', 'next_reconcile_at', 'updated_at',
                ])
                return locked
        import_status = _normalized_status(item.get('status'))
        if import_status in IMPORT_FAILED:
            return _finish_failed(
                operation.pk,
                publication_status='import_failed',
                errors=_friendly_provider_errors(
                    item.get('errors'),
                    code='import_failed',
                ),
                provider_status=_safe_text(item.get('status'), limit=100),
                response_summary={'task_status': import_status},
            )
        if import_status in IMPORT_PENDING:
            with transaction.atomic():
                locked = OzonOperation.objects.select_for_update().select_related('offer').get(
                    pk=operation.pk,
                )
                locked.response_summary = {
                    **locked.response_summary,
                    'task_status': import_status,
                }
                locked.next_reconcile_at = timezone.now() + timedelta(seconds=30)
                locked.save(update_fields=[
                    'response_summary', 'next_reconcile_at', 'updated_at',
                ])
                locked.offer.publication_status = 'import_processing'
                locked.offer.save(update_fields=['publication_status', 'updated_at'])
                return locked
        if import_status not in IMPORT_SUCCEEDED:
            return _finish_failed(
                operation.pk,
                publication_status='manual_review',
                state=OzonOperation.State.MANUAL_REVIEW,
                errors=[{
                    'code': 'unknown_import_status',
                    'message': 'Ozon вернул новый или неизвестный статус импорта.',
                }],
                provider_status=_safe_text(item.get('status'), limit=100),
                response_summary={'task_status': import_status},
            )

    try:
        product_item = client.get_product_info_by_offer_id(operation.offer.offer_id)
    except OzonAPIError as exc:
        return _persist_transient_error(operation.pk, exc)
    if product_item is None:
        return _persist_not_found(operation.pk)
    return _persist_product_projection(operation.pk, product_item)
