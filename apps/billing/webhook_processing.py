import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing.models import BillingWebhookEvent, Invoice, PaymentReversal
from apps.billing.services import BillingService, reconciliation_delay_seconds
from apps.billing.yookassa_client import (
    PaymentSnapshot,
    RefundSnapshot,
    YooKassaAPIError,
    YooKassaSnapshotError,
    fetch_payment,
    fetch_refund,
    is_valid_provider_id,
)


logger = logging.getLogger(__name__)

SUPPORTED_YOOKASSA_EVENTS = {
    'payment.succeeded',
    'payment.canceled',
    'refund.succeeded',
}
FINAL_WEBHOOK_DECISIONS = {
    BillingWebhookEvent.DECISION_APPLIED,
    BillingWebhookEvent.DECISION_IGNORED,
    BillingWebhookEvent.DECISION_REJECTED,
    BillingWebhookEvent.DECISION_MANUAL_REVIEW,
}
_TRANSIENT_PAYMENT_STATUSES = frozenset({'pending', 'waiting_for_capture'})
_TRANSIENT_REFUND_STATUSES = frozenset({'pending'})


@dataclass(frozen=True, slots=True)
class WebhookProcessResult:
    acknowledged: bool
    retry_code: str = ''


def claim_webhook_event(
    *,
    event: str,
    object_id: str,
    safe_payload: dict,
    source_ip: str | None,
) -> tuple[BillingWebhookEvent, uuid.UUID | None, str]:
    """Claims one provider delivery without holding a lock during network I/O."""
    idempotency_key = f'{event}:{object_id}'
    now = timezone.now()
    stale_before = now - timedelta(
        seconds=settings.YOOKASSA_WEBHOOK_PROCESSING_TIMEOUT_SECONDS,
    )
    with transaction.atomic():
        webhook_event, created = BillingWebhookEvent.objects.get_or_create(
            provider='yookassa',
            idempotency_key=idempotency_key,
            defaults={
                'event_type': event,
                'object_id': object_id,
                'payload': safe_payload,
                'source_ip': source_ip,
            },
        )
        if not created:
            webhook_event = BillingWebhookEvent.objects.select_for_update().get(
                pk=webhook_event.pk,
            )
            webhook_event.delivery_count += 1
            webhook_event.save(update_fields=['delivery_count', 'updated_at'])

        if webhook_event.decision in FINAL_WEBHOOK_DECISIONS:
            return webhook_event, None, 'final'
        if (
            webhook_event.processing_token is not None
            and webhook_event.processing_started_at is not None
            and webhook_event.processing_started_at > stale_before
        ):
            return webhook_event, None, 'busy'

        processing_token = uuid.uuid4()
        # Keep legacy decision=received so an old application image can safely
        # run against the expanded schema during rollback.
        webhook_event.decision = BillingWebhookEvent.DECISION_RECEIVED
        webhook_event.reason = ''
        webhook_event.processing_token = processing_token
        webhook_event.processing_started_at = now
        webhook_event.processed_at = None
        webhook_event.save(update_fields=[
            'decision', 'reason', 'processing_token', 'processing_started_at',
            'processed_at', 'updated_at',
        ])
        return webhook_event, processing_token, 'claimed'


def claim_webhook_event_for_reconciliation(
    webhook_event_id: int,
    *,
    force: bool = False,
) -> tuple[BillingWebhookEvent, uuid.UUID | None, str]:
    now = timezone.now()
    stale_before = now - timedelta(
        seconds=settings.YOOKASSA_WEBHOOK_PROCESSING_TIMEOUT_SECONDS,
    )
    with transaction.atomic():
        webhook_event = BillingWebhookEvent.objects.select_for_update().get(
            pk=webhook_event_id,
        )
        if webhook_event.decision in FINAL_WEBHOOK_DECISIONS:
            return webhook_event, None, 'final'
        if (
            webhook_event.processing_token is not None
            and webhook_event.processing_started_at is not None
            and webhook_event.processing_started_at > stale_before
        ):
            return webhook_event, None, 'busy'
        if (
            not force
            and webhook_event.next_reconciliation_at is not None
            and webhook_event.next_reconciliation_at > now
        ):
            return webhook_event, None, 'not_due'
        if (
            webhook_event.reconciliation_attempts
            >= settings.YOOKASSA_RECONCILIATION_MAX_ATTEMPTS
        ):
            # Reconciler finalizes the audit row and any related Invoice in one
            # transaction. Leaving this row non-final makes a worker crash here
            # safely retryable instead of stranding a pending Invoice.
            return webhook_event, None, 'exhausted'

        processing_token = uuid.uuid4()
        webhook_event.reconciliation_attempts += 1
        webhook_event.last_reconciliation_at = now
        webhook_event.decision = BillingWebhookEvent.DECISION_RECEIVED
        webhook_event.processing_token = processing_token
        webhook_event.processing_started_at = now
        webhook_event.processed_at = None
        webhook_event.save(update_fields=[
            'reconciliation_attempts', 'last_reconciliation_at', 'decision',
            'processing_token', 'processing_started_at', 'processed_at',
            'updated_at',
        ])
        return webhook_event, processing_token, 'claimed'


def finalize_webhook_event(
    *,
    webhook_event_id: int,
    processing_token: uuid.UUID,
    decision: str,
    reason: str = '',
    invoice: Invoice | None = None,
    payment_id: str = '',
    amount: Decimal | None = None,
    currency: str = '',
) -> str:
    """Finalizes only the current claim; a stale worker cannot win a retry race."""
    with transaction.atomic():
        webhook_event = BillingWebhookEvent.objects.select_for_update().get(
            pk=webhook_event_id,
        )
        if webhook_event.decision in FINAL_WEBHOOK_DECISIONS:
            return webhook_event.decision
        if webhook_event.processing_token != processing_token:
            return webhook_event.decision

        now = timezone.now()
        webhook_event.invoice = invoice
        webhook_event.tenant = invoice.tenant if invoice is not None else None
        webhook_event.payment_id = payment_id
        webhook_event.amount = amount
        webhook_event.currency = currency
        webhook_event.decision = decision
        webhook_event.reason = reason[:500]
        webhook_event.processing_token = None
        webhook_event.processing_started_at = None
        webhook_event.processed_at = now
        webhook_event.next_reconciliation_at = (
            now + timedelta(
                seconds=reconciliation_delay_seconds(
                    max(1, webhook_event.reconciliation_attempts),
                ),
            )
            if decision == BillingWebhookEvent.DECISION_ERROR
            else None
        )
        webhook_event.save(update_fields=[
            'invoice', 'tenant', 'payment_id', 'amount', 'currency',
            'decision', 'reason', 'processing_token', 'processing_started_at',
            'processed_at', 'next_reconciliation_at', 'updated_at',
        ])
        return webhook_event.decision


def _final_result(actual_decision: str, retry_code: str) -> WebhookProcessResult:
    if actual_decision in FINAL_WEBHOOK_DECISIONS:
        return WebhookProcessResult(True)
    return WebhookProcessResult(False, retry_code)


def _finalize_status_mismatch(
    *,
    webhook_event: BillingWebhookEvent,
    processing_token: uuid.UUID,
    observed_status: str,
    transient_statuses: frozenset[str],
    payment_id: str,
    amount: Decimal,
    currency: str,
    subject: str,
    retry_code: str,
) -> WebhookProcessResult:
    """Retry only states that can still converge; close terminal contradictions."""
    is_transient = observed_status in transient_statuses
    actual = finalize_webhook_event(
        webhook_event_id=webhook_event.pk,
        processing_token=processing_token,
        decision=(
            BillingWebhookEvent.DECISION_ERROR
            if is_transient
            else BillingWebhookEvent.DECISION_IGNORED
        ),
        reason=(
            f'Авторитетный статус {subject} пока не соответствует событию.'
            if is_transient
            else (
                f'Событие противоречит терминальному авторитетному статусу '
                f'{subject}: {observed_status}.'
            )
        ),
        payment_id=payment_id,
        amount=amount,
        currency=currency,
    )
    return _final_result(actual, retry_code)


def process_claimed_yookassa_event(
    webhook_event_id: int,
    processing_token: uuid.UUID,
) -> WebhookProcessResult:
    webhook_event = BillingWebhookEvent.objects.get(pk=webhook_event_id)
    event = webhook_event.event_type
    object_id = webhook_event.object_id

    if event not in SUPPORTED_YOOKASSA_EVENTS:
        finalize_webhook_event(
            webhook_event_id=webhook_event.pk,
            processing_token=processing_token,
            decision=BillingWebhookEvent.DECISION_IGNORED,
            reason='Неподдерживаемый тип события.',
        )
        return WebhookProcessResult(True)
    if not is_valid_provider_id(object_id):
        finalize_webhook_event(
            webhook_event_id=webhook_event.pk,
            processing_token=processing_token,
            decision=BillingWebhookEvent.DECISION_REJECTED,
            reason='В webhook отсутствует корректный идентификатор объекта.',
        )
        return WebhookProcessResult(True)

    refund_snapshot: RefundSnapshot | None = None
    try:
        if event == 'refund.succeeded':
            refund_snapshot = fetch_refund(object_id)
            provider_snapshot: PaymentSnapshot | RefundSnapshot = refund_snapshot
            payment_snapshot = fetch_payment(refund_snapshot.payment_id)
        else:
            payment_snapshot = fetch_payment(object_id)
            provider_snapshot = payment_snapshot
    except (YooKassaAPIError, YooKassaSnapshotError):
        logger.warning(
            'Не удалось подтвердить YooKassa %s %s.',
            event,
            object_id,
            exc_info=True,
        )
        actual = finalize_webhook_event(
            webhook_event_id=webhook_event.pk,
            processing_token=processing_token,
            decision=BillingWebhookEvent.DECISION_ERROR,
            reason='Не удалось подтвердить объект через YooKassa API.',
        )
        return _final_result(actual, 'provider_unavailable')
    except Exception:
        logger.exception('Непредвиденная ошибка чтения YooKassa %s %s.', event, object_id)
        actual = finalize_webhook_event(
            webhook_event_id=webhook_event.pk,
            processing_token=processing_token,
            decision=BillingWebhookEvent.DECISION_ERROR,
            reason='Не удалось подтвердить объект через YooKassa API.',
        )
        return _final_result(actual, 'provider_unavailable')

    expected_status = event.partition('.')[2]
    payment_id = (
        refund_snapshot.payment_id
        if refund_snapshot is not None
        else payment_snapshot.id
    )
    if provider_snapshot.status != expected_status:
        return _finalize_status_mismatch(
            webhook_event=webhook_event,
            processing_token=processing_token,
            observed_status=provider_snapshot.status,
            transient_statuses=(
                _TRANSIENT_REFUND_STATUSES
                if event == 'refund.succeeded'
                else _TRANSIENT_PAYMENT_STATUSES
            ),
            payment_id=payment_id,
            amount=provider_snapshot.amount,
            currency=provider_snapshot.currency,
            subject='объекта',
            retry_code='provider_state_mismatch',
        )
    if event == 'refund.succeeded' and payment_snapshot.status != 'succeeded':
        return _finalize_status_mismatch(
            webhook_event=webhook_event,
            processing_token=processing_token,
            observed_status=payment_snapshot.status,
            transient_statuses=_TRANSIENT_PAYMENT_STATUSES,
            payment_id=payment_id,
            amount=provider_snapshot.amount,
            currency=provider_snapshot.currency,
            subject='связанного платежа',
            retry_code='linked_payment_state_mismatch',
        )

    try:
        invoice = Invoice.objects.select_related('tenant').get(
            yookassa_payment_id=payment_id,
        )
    except Invoice.DoesNotExist:
        actual = finalize_webhook_event(
            webhook_event_id=webhook_event.pk,
            processing_token=processing_token,
            decision=BillingWebhookEvent.DECISION_ERROR,
            reason='Invoice для подтверждённого платежа пока не найден.',
            payment_id=payment_id,
            amount=provider_snapshot.amount,
            currency=provider_snapshot.currency,
        )
        return _final_result(actual, 'invoice_not_ready')
    except Invoice.MultipleObjectsReturned:
        actual = finalize_webhook_event(
            webhook_event_id=webhook_event.pk,
            processing_token=processing_token,
            decision=BillingWebhookEvent.DECISION_ERROR,
            reason='Платёж связан более чем с одним Invoice.',
            payment_id=payment_id,
            amount=provider_snapshot.amount,
            currency=provider_snapshot.currency,
        )
        return _final_result(actual, 'ambiguous_invoice')

    if payment_snapshot.test and not settings.YOOKASSA_ALLOW_TEST_PAYMENTS:
        reason = 'Тестовый платёж не активирует товары или баланс.'
        try:
            invoice = BillingService.mark_invoice_manual_review(invoice.pk, reason)
        except Exception:
            logger.exception('Не удалось зафиксировать test payment invoice=%s.', invoice.pk)
            actual = finalize_webhook_event(
                webhook_event_id=webhook_event.pk,
                processing_token=processing_token,
                decision=BillingWebhookEvent.DECISION_ERROR,
                reason='Ошибка фиксации тестового платежа.',
                invoice=invoice,
                payment_id=payment_id,
                amount=provider_snapshot.amount,
                currency=provider_snapshot.currency,
            )
            return _final_result(actual, 'manual_review_write_failed')
        finalize_webhook_event(
            webhook_event_id=webhook_event.pk,
            processing_token=processing_token,
            decision=BillingWebhookEvent.DECISION_MANUAL_REVIEW,
            reason=reason,
            invoice=invoice,
            payment_id=payment_id,
            amount=provider_snapshot.amount,
            currency=provider_snapshot.currency,
        )
        return WebhookProcessResult(True)

    if (
        provider_snapshot.currency != invoice.currency
        or payment_snapshot.currency != invoice.currency
        or payment_snapshot.amount != invoice.amount
        or (event.startswith('payment.') and provider_snapshot.amount != invoice.amount)
    ):
        reason = 'Сумма или валюта авторитетного объекта не совпадает с Invoice.'
        try:
            invoice = BillingService.mark_invoice_manual_review(
                invoice.pk,
                reason,
                refund_review=event == 'refund.succeeded',
            )
        except Exception:
            logger.exception('Invoice %s не удалось перевести в manual review.', invoice.pk)
            actual = finalize_webhook_event(
                webhook_event_id=webhook_event.pk,
                processing_token=processing_token,
                decision=BillingWebhookEvent.DECISION_ERROR,
                reason='Ошибка фиксации financial mismatch.',
                invoice=invoice,
                payment_id=payment_id,
                amount=provider_snapshot.amount,
                currency=provider_snapshot.currency,
            )
            return _final_result(actual, 'manual_review_write_failed')
        finalize_webhook_event(
            webhook_event_id=webhook_event.pk,
            processing_token=processing_token,
            decision=BillingWebhookEvent.DECISION_MANUAL_REVIEW,
            reason=reason,
            invoice=invoice,
            payment_id=payment_id,
            amount=provider_snapshot.amount,
            currency=provider_snapshot.currency,
        )
        return WebhookProcessResult(True)

    try:
        reason = ''
        if event == 'payment.succeeded':
            processed = BillingService.handle_payment_success_webhook(
                invoice.pk,
                payment_id=payment_snapshot.id,
                amount=payment_snapshot.amount,
                currency=payment_snapshot.currency,
            )
            if processed:
                decision = BillingWebhookEvent.DECISION_APPLIED
            else:
                invoice.refresh_from_db()
                reason = (
                    invoice.checkout_last_error
                    or 'Оплаченный Invoice не прошёл повторную проверку.'
                )
                if invoice.status != Invoice.STATUS_MANUAL_REVIEW:
                    invoice = BillingService.mark_invoice_manual_review(
                        invoice.pk,
                        reason,
                    )
                decision = BillingWebhookEvent.DECISION_MANUAL_REVIEW
        elif event == 'payment.canceled':
            processed = BillingService.handle_payment_failed_webhook(
                invoice.pk,
                payment_id=payment_snapshot.id,
            )
            if processed:
                decision = BillingWebhookEvent.DECISION_APPLIED
            else:
                invoice.refresh_from_db()
                reason = 'Статус Invoice не согласуется с отменённым платежом.'
                if invoice.status != Invoice.STATUS_MANUAL_REVIEW:
                    invoice = BillingService.mark_invoice_manual_review(
                        invoice.pk,
                        reason,
                    )
                decision = BillingWebhookEvent.DECISION_MANUAL_REVIEW
        else:
            if refund_snapshot is None:
                raise RuntimeError('Refund webhook не содержит refund snapshot.')
            reversal = BillingService.handle_reversal_success(
                invoice_id=invoice.pk,
                provider_reference=refund_snapshot.id,
                payment_id=refund_snapshot.payment_id,
                amount=refund_snapshot.amount,
                currency=refund_snapshot.currency,
            )
            if reversal is None:
                reason = 'Подтверждённый возврат не удалось применить.'
                invoice = BillingService.mark_invoice_manual_review(
                    invoice.pk,
                    reason,
                    refund_review=True,
                )
                decision = BillingWebhookEvent.DECISION_MANUAL_REVIEW
            else:
                decision = (
                    BillingWebhookEvent.DECISION_APPLIED
                    if reversal.status == PaymentReversal.STATUS_APPLIED
                    else BillingWebhookEvent.DECISION_MANUAL_REVIEW
                )
                reason = reversal.reason
    except Exception:
        logger.exception('Ошибка применения YooKassa %s %s.', event, object_id)
        actual = finalize_webhook_event(
            webhook_event_id=webhook_event.pk,
            processing_token=processing_token,
            decision=BillingWebhookEvent.DECISION_ERROR,
            reason='Временная ошибка применения подтверждённого события.',
            invoice=invoice,
            payment_id=payment_id,
            amount=provider_snapshot.amount,
            currency=provider_snapshot.currency,
        )
        return _final_result(actual, 'processing_error')

    actual = finalize_webhook_event(
        webhook_event_id=webhook_event.pk,
        processing_token=processing_token,
        decision=decision,
        reason=reason,
        invoice=invoice,
        payment_id=payment_id,
        amount=provider_snapshot.amount,
        currency=provider_snapshot.currency,
    )
    return _final_result(actual, 'claim_lost')
