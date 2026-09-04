"""Durable, fail-closed manual publication of one Ozon offer."""

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from typing import Any

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.datasources.encryption import decrypt
from apps.marketplaces.adapters.ozon.client import OzonAPIError, OzonSellerClient
from apps.marketplaces.models import (
    MarketplaceAccount,
    OzonAccountProfile,
    OzonOfferDraft,
    OzonOperation,
)
from apps.marketplaces.ozon_offers import offer_presentation
from apps.marketplaces.ozon_rollout import ozon_product_write_enabled_for_account
from apps.products.media import get_product_image_delivery_key, get_publishable_product_images
from apps.products.models import Product
from apps.products.physical_profiles import physical_profile_presentation


class OzonPublicationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def ozon_barcode_generation_enabled_for_account(account) -> bool:
    """Require product writes plus the exact barcode endpoint permission."""

    return (
        ozon_product_write_enabled_for_account(account)
        and '/v1/barcode/generate' in account.ozon_profile.api_methods
    )


def _public_image_url(image) -> str:
    key = get_product_image_delivery_key(image)
    cdn = str(getattr(settings, 'YC_CDN_DOMAIN', '') or '').strip().strip('/')
    if cdn and key and hasattr(default_storage, 'bucket_name'):
        return f'https://{cdn}/{key}'
    if key:
        return str(default_storage.url(key))
    return str(image.url_source or '')


def _positive_integer(value: Any, label: str) -> int:
    try:
        normalized = Decimal(str(value)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OzonPublicationError('invalid_physical_value', f'{label}: проверьте значение.') from exc
    if normalized <= 0:
        raise OzonPublicationError('invalid_physical_value', f'{label}: значение должно быть больше нуля.')
    return int(normalized)


def _vat_value(value: Any) -> str | None:
    if value in (None, ''):
        return None
    try:
        normalized = Decimal(str(value)) / Decimal('100')
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OzonPublicationError('invalid_vat', 'Проверьте ставку НДС.') from exc
    return format(normalized.normalize(), 'f')


def provider_barcodes_from_item(item: Mapping[str, Any]) -> list[str]:
    raw = item.get('barcodes')
    candidates = raw if isinstance(raw, list) else []
    legacy = item.get('barcode')
    if isinstance(legacy, str) and legacy.strip():
        candidates = [legacy, *candidates]
    barcodes: list[str] = []
    for value in candidates[:50]:
        barcode = str(value or '').strip()
        if barcode and len(barcode) <= 100 and barcode not in barcodes:
            barcodes.append(barcode)
    return barcodes


def build_product_import_item(
    product: Product,
    account: MarketplaceAccount,
    draft: OzonOfferDraft,
) -> dict[str, Any]:
    """Build the complete provider item only after the local preflight passes."""

    presentation = offer_presentation(product, account)
    if not presentation['preflight']['ready']:
        raise OzonPublicationError(
            'preflight_failed',
            'Карточка Ozon не готова. Исправьте обязательные поля перед отправкой.',
        )
    pricing = presentation.get('pricing')
    if not isinstance(pricing, Mapping):
        raise OzonPublicationError('price_missing', 'Не удалось рассчитать цену Ozon.')
    physical = physical_profile_presentation(product)['facts']
    images = [
        url
        for image in get_publishable_product_images(product)[:15]
        if (url := _public_image_url(image)).startswith('https://')
    ]
    if not images:
        raise OzonPublicationError(
            'image_missing',
            'Для отправки в Ozon нужна хотя бы одна доступная HTTPS-фотография.',
        )

    def required_fact(name: str) -> Any:
        value = physical[name]['effective_value']
        if value in (None, ''):
            raise OzonPublicationError(
                'physical_fact_missing',
                'Заполните размеры и вес упаковки перед отправкой в Ozon.',
            )
        return value

    profile = OzonAccountProfile.objects.filter(account=account).first()
    item: dict[str, Any] = {
        'attributes': draft.attributes,
        'currency_code': (profile.currency if profile and profile.currency else 'RUB')[:10],
        'depth': _positive_integer(required_fact('length_mm'), 'Длина'),
        'description_category_id': draft.description_category_id,
        'dimension_unit': 'mm',
        'height': _positive_integer(required_fact('height_mm'), 'Высота'),
        'images': images,
        'name': (product.title_ai or product.name).strip()[:500],
        'description': (product.description_ai or '').strip()[:6000],
        'offer_id': draft.offer_id,
        'price': str(pricing['final_price']),
        'primary_image': images[0],
        'type_id': draft.type_id,
        'weight': _positive_integer(required_fact('weight_g'), 'Вес'),
        'weight_unit': 'g',
        'width': _positive_integer(required_fact('width_mm'), 'Ширина'),
    }
    barcode = str(physical['barcode']['effective_value'] or '').strip()
    if barcode:
        item['barcode'] = barcode
    vat = _vat_value(physical['vat_rate']['effective_value'])
    if vat is not None:
        item['vat'] = vat
    return item


def _request_digest(item: dict[str, Any]) -> str:
    payload = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()


def operation_presentation(operation: OzonOperation | None) -> dict[str, Any] | None:
    if operation is None:
        return None
    return {
        'id': str(operation.pk),
        'kind': operation.kind,
        'state': operation.state,
        'provider_task_id': operation.provider_task_id or None,
        'errors': operation.errors if isinstance(operation.errors, list) else [],
        'attempt_count': operation.attempt_count,
        'reconcile_count': operation.reconcile_count,
        'last_reconciled_at': operation.last_reconciled_at,
        'next_reconcile_at': operation.next_reconcile_at,
        'retry_after_at': operation.retry_after_at,
        'completed_at': operation.completed_at,
        'created_at': operation.created_at,
        'updated_at': operation.updated_at,
    }


def latest_offer_operation(draft: OzonOfferDraft | None) -> OzonOperation | None:
    if draft is None or draft.pk is None:
        return None
    return draft.operations.filter(
        kind__in=(OzonOperation.Kind.PRODUCT_IMPORT, OzonOperation.Kind.ARCHIVE),
    ).order_by('-created_at').first()


def _validated_credentials(account: MarketplaceAccount) -> tuple[str, str]:
    try:
        credentials = decrypt(account.credentials_enc)
    except Exception as exc:
        raise OzonPublicationError(
            'credentials_unavailable',
            'Не удалось прочитать защищённые credentials Ozon.',
        ) from exc
    client_id = str(credentials.get('client_id') or '').strip()
    api_key = str(credentials.get('api_key') or '').strip()
    if client_id != account.external_id or not api_key:
        raise OzonPublicationError(
            'credentials_invalid',
            'Повторно подключите API-ключ выбранного кабинета Ozon.',
        )
    return client_id, api_key


def _require_write_admission(account: MarketplaceAccount) -> None:
    if account.marketplace != MarketplaceAccount.MARKETPLACE_OZON:
        raise OzonPublicationError('account_invalid', 'Выберите кабинет Ozon.')
    if not account.is_active or not ozon_product_write_enabled_for_account(account):
        raise OzonPublicationError(
            'write_disabled',
            'Отправка товаров выключена для этого кабинета Ozon.',
        )
    try:
        profile = account.ozon_profile
    except OzonAccountProfile.DoesNotExist as exc:
        raise OzonPublicationError('account_not_ready', 'Сначала проверьте подключение Ozon.') from exc
    if profile.connection_status != OzonAccountProfile.ConnectionStatus.CONNECTED:
        raise OzonPublicationError('account_not_ready', 'Сначала проверьте подключение Ozon.')


def request_product_barcode_generation(
    product: Product,
    account: MarketplaceAccount,
) -> OzonOfferDraft:
    """Generate an account-scoped Ozon barcode without inventing a GTIN in MAP."""

    now = timezone.now()
    with transaction.atomic():
        locked_account = MarketplaceAccount.objects.select_for_update().select_related(
            'tenant',
        ).get(pk=account.pk, tenant=product.tenant)
        _require_write_admission(locked_account)
        if not ozon_barcode_generation_enabled_for_account(locked_account):
            raise OzonPublicationError(
                'barcode_generation_disabled',
                'API-ключ Ozon не разрешает создание штрихкодов. '
                'Переподключите ключ с правом /v1/barcode/generate.',
            )
        locked_product = Product.objects.select_for_update().get(
            pk=product.pk,
            tenant=product.tenant,
        )
        draft = OzonOfferDraft.objects.select_for_update().filter(
            tenant=product.tenant,
            product=locked_product,
            account=locked_account,
        ).first()
        if draft is None or draft.provider_product_id is None:
            raise OzonPublicationError(
                'provider_product_missing',
                'Сначала отправьте карточку и дождитесь Product ID от Ozon.',
            )
        if draft.provider_barcodes:
            return draft
        common_barcode = str(
            physical_profile_presentation(locked_product)['facts']['barcode'][
                'effective_value'
            ] or '',
        ).strip()
        if common_barcode:
            raise OzonPublicationError(
                'barcode_already_available',
                'У товара уже есть штрихкод из 1С или MAP. Отправьте обновление карточки в Ozon.',
            )
        should_generate = draft.barcode_generation_status in {
            OzonOfferDraft.BarcodeGenerationStatus.NOT_REQUESTED,
            OzonOfferDraft.BarcodeGenerationStatus.FAILED,
        }
        if draft.barcode_generation_status == OzonOfferDraft.BarcodeGenerationStatus.REQUESTING:
            if (
                draft.barcode_generation_requested_at is not None
                and draft.barcode_generation_requested_at > now - timedelta(minutes=2)
            ):
                return draft
            should_generate = False
            draft.barcode_generation_status = (
                OzonOfferDraft.BarcodeGenerationStatus.OUTCOME_UNKNOWN
            )
        if should_generate:
            draft.barcode_generation_status = OzonOfferDraft.BarcodeGenerationStatus.REQUESTING
            draft.barcode_generation_error = ''
            draft.barcode_generation_requested_at = now
            draft.save(update_fields=[
                'barcode_generation_status',
                'barcode_generation_error',
                'barcode_generation_requested_at',
                'updated_at',
            ])
        product_id = draft.provider_product_id
        draft_id = draft.pk
        client_id, api_key = _validated_credentials(locked_account)

    client = OzonSellerClient(client_id=client_id, api_key=api_key)
    generation_error: OzonAPIError | None = None
    provider_errors: list[dict[str, Any]] = []
    if should_generate:
        try:
            provider_errors = client.generate_barcodes([product_id])
        except OzonAPIError as exc:
            generation_error = exc

    product_item: dict[str, Any] | None = None
    try:
        product_item = client.get_product_info_by_offer_id(draft.offer_id)
    except OzonAPIError:
        product_item = None
    barcodes = provider_barcodes_from_item(product_item or {})

    with transaction.atomic():
        draft = OzonOfferDraft.objects.select_for_update().get(
            pk=draft_id,
            tenant=product.tenant,
            account=account,
        )
        if draft.provider_product_id != product_id:
            raise OzonPublicationError(
                'provider_product_changed',
                'Product ID Ozon изменился во время запроса. Обновите карточку.',
            )
        if barcodes:
            draft.provider_barcodes = barcodes
            draft.barcode_generation_status = OzonOfferDraft.BarcodeGenerationStatus.READY
            draft.barcode_generation_error = ''
        elif provider_errors:
            draft.barcode_generation_status = OzonOfferDraft.BarcodeGenerationStatus.FAILED
            draft.barcode_generation_error = (
                'Ozon не создал штрихкод для текущего состояния товара. '
                'Проверьте статус карточки и повторите после исправления.'
            )
        elif generation_error is not None:
            draft.barcode_generation_status = (
                OzonOfferDraft.BarcodeGenerationStatus.OUTCOME_UNKNOWN
                if generation_error.code in {'connection_error', 'provider_unavailable'}
                else OzonOfferDraft.BarcodeGenerationStatus.FAILED
            )
            draft.barcode_generation_error = str(generation_error)[:500]
        elif should_generate:
            draft.barcode_generation_status = OzonOfferDraft.BarcodeGenerationStatus.REQUESTED
            draft.barcode_generation_error = ''
        draft.save(update_fields=[
            'provider_barcodes',
            'barcode_generation_status',
            'barcode_generation_error',
            'updated_at',
        ])
        return draft


def request_product_import(
    product: Product,
    account: MarketplaceAccount,
    *,
    idempotency_key: str,
) -> OzonOperation:
    """Create exactly one durable operation, then perform one provider POST."""

    normalized_key = str(idempotency_key).strip()
    if not normalized_key or len(normalized_key) > 100:
        raise OzonPublicationError('idempotency_key_invalid', 'Повторите отправку карточки.')

    with transaction.atomic():
        locked_account = MarketplaceAccount.objects.select_for_update().select_related(
            'tenant',
        ).get(pk=account.pk, tenant=product.tenant)
        locked_product = Product.objects.select_for_update().get(
            pk=product.pk,
            tenant=product.tenant,
        )
        _require_write_admission(locked_account)
        draft = OzonOfferDraft.objects.select_for_update().filter(
            tenant=product.tenant,
            product=locked_product,
            account=locked_account,
        ).first()
        if draft is None:
            raise OzonPublicationError('draft_missing', 'Сначала подготовьте карточку Ozon.')
        existing = OzonOperation.objects.filter(
            account=locked_account,
            idempotency_key=normalized_key,
        ).first()
        if existing is not None:
            if existing.offer_id != draft.pk:
                raise OzonPublicationError(
                    'idempotency_conflict',
                    'Идентификатор отправки уже использован для другой карточки Ozon.',
                )
            return existing
        archive_operation = OzonOperation.objects.filter(
            offer=draft,
            kind=OzonOperation.Kind.ARCHIVE,
        ).order_by('-created_at').first()
        if draft.publication_status == 'archived' or (
            archive_operation is not None
            and archive_operation.state in OzonOperation.ACTIVE_STATES
        ):
            raise OzonPublicationError(
                'offer_archived',
                'Карточка снимается или уже находится в архиве Ozon. '
                'Для повторной публикации сначала нужно вернуть её из архива.',
            )
        active = OzonOperation.objects.filter(
            offer=draft,
            kind=OzonOperation.Kind.PRODUCT_IMPORT,
            state__in=OzonOperation.ACTIVE_STATES,
        ).order_by('-created_at').first()
        if active is not None:
            return active
        latest = OzonOperation.objects.filter(
            offer=draft,
            kind=OzonOperation.Kind.PRODUCT_IMPORT,
        ).order_by('-created_at').first()
        if latest is not None and latest.state in {
            OzonOperation.State.PARTIAL,
            OzonOperation.State.MANUAL_REVIEW,
        }:
            raise OzonPublicationError(
                'manual_review_required',
                'Сначала завершите ручную проверку предыдущей операции Ozon.',
            )
        if (
            latest is not None
            and latest.retry_after_at is not None
            and latest.retry_after_at > timezone.now()
        ):
            raise OzonPublicationError(
                'retry_later',
                'Ozon ограничил частоту запросов. Повторите после времени, указанного в карточке.',
            )
        item = build_product_import_item(locked_product, locked_account, draft)
        request_sha256 = _request_digest(item)
        if (
            latest is not None
            and latest.state == OzonOperation.State.FAILED
            and latest.request_sha256 == request_sha256
            and any(
                isinstance(error, Mapping)
                and error.get('code') in {
                    'import_failed',
                    'moderation_rejected',
                    'request_rejected',
                }
                for error in latest.errors
            )
        ):
            raise OzonPublicationError(
                'correction_required',
                'Ozon уже отклонил эту версию карточки. Исправьте указанные поля и сохраните изменения.',
            )
        try:
            with transaction.atomic():
                operation = OzonOperation.objects.create(
                    tenant=product.tenant,
                    account=locked_account,
                    offer=draft,
                    kind=OzonOperation.Kind.PRODUCT_IMPORT,
                    idempotency_key=normalized_key,
                    request_sha256=request_sha256,
                    request_summary={
                        'offer_id': draft.offer_id,
                        'description_category_id': draft.description_category_id,
                        'type_id': draft.type_id,
                        'price': item['price'],
                        'attribute_count': len(item['attributes']),
                        'image_count': len(item['images']),
                    },
                )
        except IntegrityError:
            raced = OzonOperation.objects.get(
                account=locked_account,
                idempotency_key=normalized_key,
            )
            if raced.offer_id != draft.pk:
                raise OzonPublicationError(
                    'idempotency_conflict',
                    'Идентификатор отправки уже использован для другой карточки Ozon.',
                )
            return raced
        draft.publication_status = 'queued'
        draft.provider_errors = []
        draft.save(update_fields=['publication_status', 'provider_errors', 'updated_at'])

    return _submit_product_import(operation.pk, item=item)


def _submit_product_import(operation_id, *, item: dict[str, Any] | None = None) -> OzonOperation:
    with transaction.atomic():
        operation = OzonOperation.objects.select_for_update().select_related(
            'account', 'account__tenant', 'offer', 'offer__product',
        ).get(pk=operation_id)
        if operation.state != OzonOperation.State.QUEUED:
            return operation
        if item is None:
            item = build_product_import_item(
                operation.offer.product,
                operation.account,
                operation.offer,
            )
            if _request_digest(item) != operation.request_sha256:
                operation.state = OzonOperation.State.MANUAL_REVIEW
                operation.errors = [{
                    'code': 'desired_state_changed',
                    'message': 'Карточка изменилась после постановки в очередь. Запустите отправку снова.',
                }]
                operation.completed_at = timezone.now()
                operation.save(update_fields=['state', 'errors', 'completed_at', 'updated_at'])
                return operation
        operation.state = OzonOperation.State.SENDING
        operation.attempt_count += 1
        operation.last_attempt_at = timezone.now()
        operation.save(update_fields=['state', 'attempt_count', 'last_attempt_at', 'updated_at'])
        try:
            client_id, api_key = _validated_credentials(operation.account)
        except OzonPublicationError as exc:
            operation.state = OzonOperation.State.FAILED
            operation.errors = [{'code': exc.code, 'message': str(exc)}]
            operation.completed_at = timezone.now()
            operation.save(update_fields=['state', 'errors', 'completed_at', 'updated_at'])
            operation.offer.publication_status = 'send_failed'
            operation.offer.save(update_fields=['publication_status', 'updated_at'])
            return operation

    try:
        task_id = OzonSellerClient(client_id=client_id, api_key=api_key).import_products([item])
    except OzonAPIError as exc:
        with transaction.atomic():
            operation = OzonOperation.objects.select_for_update().select_related('offer').get(
                pk=operation_id,
            )
            unknown = exc.code in {'connection_error', 'provider_unavailable'}
            operation.state = (
                OzonOperation.State.OUTCOME_UNKNOWN
                if unknown else OzonOperation.State.FAILED
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
            operation.offer.publication_status = (
                'outcome_unknown' if unknown else 'send_failed'
            )
            operation.offer.save(update_fields=['publication_status', 'updated_at'])
        return operation

    with transaction.atomic():
        operation = OzonOperation.objects.select_for_update().select_related('offer').get(
            pk=operation_id,
        )
        operation.state = OzonOperation.State.RECONCILING
        operation.provider_task_id = task_id
        operation.response_summary = {'task_id': task_id}
        operation.errors = []
        operation.save(update_fields=[
            'state', 'provider_task_id', 'response_summary', 'errors', 'updated_at',
        ])
        operation.offer.publication_status = 'import_processing'
        operation.offer.save(update_fields=['publication_status', 'updated_at'])
    return operation
