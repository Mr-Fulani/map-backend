import logging
from dataclasses import asdict, dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.billing.models import BillingWebhookEvent, Invoice
from apps.billing.services import (
    BillingService,
    CheckoutManualReviewError,
    CheckoutPendingError,
    CheckoutTerminalError,
    _is_unsettled_checkout_manual,
    reconciliation_delay_seconds,
)
from apps.billing.webhook_processing import (
    claim_webhook_event,
    claim_webhook_event_for_reconciliation,
    process_claimed_yookassa_event,
)
from apps.billing.yookassa_client import (
    YooKassaAPIError,
    YooKassaSnapshotError,
    fetch_payment,
)


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ReconciliationStats:
    events_claimed: int = 0
    events_final: int = 0
    events_retryable: int = 0
    invoices_claimed: int = 0
    invoices_resumed: int = 0
    invoices_waiting: int = 0
    invoices_final: int = 0
    manual_review: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@transaction.atomic
def _finalize_exhausted_event(webhook_event_id: int) -> bool | None:
    """Atomically closes exhausted audit and marks only an incompatible Invoice."""
    event = BillingWebhookEvent.objects.select_for_update().get(pk=webhook_event_id)
    if event.decision in {
        BillingWebhookEvent.DECISION_APPLIED,
        BillingWebhookEvent.DECISION_IGNORED,
        BillingWebhookEvent.DECISION_REJECTED,
        BillingWebhookEvent.DECISION_MANUAL_REVIEW,
    }:
        return False
    now = timezone.now()
    fresh_after = now - timedelta(
        seconds=settings.YOOKASSA_WEBHOOK_PROCESSING_TIMEOUT_SECONDS,
    )
    if (
        event.reconciliation_attempts
        < settings.YOOKASSA_RECONCILIATION_MAX_ATTEMPTS
        or (
            event.processing_token is not None
            and event.processing_started_at is not None
            and event.processing_started_at > fresh_after
        )
    ):
        # A fresh direct webhook reclaimed the row after the exhausted check.
        return None

    lookup_payment_id = event.payment_id
    if not lookup_payment_id and event.event_type.startswith('payment.'):
        lookup_payment_id = event.object_id
    invoice_query = Invoice.objects.select_for_update().select_related('tenant')
    invoice = None
    if event.invoice_id is not None:
        invoice = invoice_query.filter(pk=event.invoice_id).first()
    elif lookup_payment_id:
        invoice = invoice_query.filter(
            yookassa_payment_id=lookup_payment_id,
        ).first()

    compatible_statuses = {
        'payment.succeeded': {
            Invoice.STATUS_PAID,
            Invoice.STATUS_PARTIALLY_REFUNDED,
            Invoice.STATUS_REFUNDED,
        },
        'payment.canceled': {Invoice.STATUS_FAILED},
        'refund.succeeded': {
            Invoice.STATUS_PARTIALLY_REFUNDED,
            Invoice.STATUS_REFUNDED,
        },
    }
    marked_manual = False
    reason = 'Исчерпан лимит автоматической сверки YooKassa.'
    if invoice is not None and invoice.status not in compatible_statuses.get(
        event.event_type,
        set(),
    ):
        BillingService._mark_invoice_manual_review_locked(
            invoice,
            reason,
            refund_review=event.event_type == 'refund.succeeded',
        )
        marked_manual = True

    event.invoice = invoice
    event.tenant = invoice.tenant if invoice is not None else None
    event.decision = BillingWebhookEvent.DECISION_MANUAL_REVIEW
    event.reason = reason
    event.processing_token = None
    event.processing_started_at = None
    event.next_reconciliation_at = None
    event.processed_at = now
    event.save(update_fields=[
        'invoice', 'tenant', 'decision', 'reason', 'processing_token',
        'processing_started_at', 'next_reconciliation_at', 'processed_at',
        'updated_at',
    ])
    return marked_manual


def _event_candidate_ids(
    *,
    limit: int,
    event_ids: list[int] | None,
    force: bool,
) -> list[int]:
    queryset = BillingWebhookEvent.objects.filter(provider='yookassa')
    if event_ids is not None:
        queryset = queryset.filter(pk__in=event_ids)
    else:
        now = timezone.now()
        stale_before = now - timedelta(
            seconds=settings.YOOKASSA_WEBHOOK_PROCESSING_TIMEOUT_SECONDS,
        )
        queryset = queryset.filter(
            Q(decision=BillingWebhookEvent.DECISION_ERROR)
            | Q(
                decision=BillingWebhookEvent.DECISION_RECEIVED,
                processing_started_at__lt=stale_before,
            )
            | Q(
                decision=BillingWebhookEvent.DECISION_RECEIVED,
                processing_started_at__isnull=True,
            ),
        )
        if not force:
            queryset = queryset.filter(
                Q(next_reconciliation_at__isnull=True)
                | Q(next_reconciliation_at__lte=now),
            )
    return list(queryset.order_by('created_at').values_list('pk', flat=True)[:limit])


def _reconcile_events(
    stats: ReconciliationStats,
    *,
    limit: int,
    event_ids: list[int] | None,
    force: bool,
) -> None:
    for event_id in _event_candidate_ids(
        limit=limit,
        event_ids=event_ids,
        force=force,
    ):
        try:
            event, token, claim_state = claim_webhook_event_for_reconciliation(
                event_id,
                force=force,
            )
        except BillingWebhookEvent.DoesNotExist:
            continue
        if claim_state == 'exhausted':
            try:
                exhausted_result = _finalize_exhausted_event(event.pk)
                if exhausted_result is None:
                    continue
                if exhausted_result:
                    stats.manual_review += 1
            except BillingWebhookEvent.DoesNotExist:
                continue
            stats.events_final += 1
            continue
        if claim_state == 'final':
            stats.events_final += 1
            continue
        if claim_state != 'claimed' or token is None:
            continue

        stats.events_claimed += 1
        try:
            result = process_claimed_yookassa_event(event.pk, token)
        except Exception:
            logger.exception('Ошибка фоновой обработки webhook event=%s.', event.pk)
            stats.errors += 1
            continue
        if result.acknowledged:
            stats.events_final += 1
        else:
            stats.events_retryable += 1


def _invoice_candidate_ids(
    *,
    limit: int,
    invoice_ids: list[int] | None,
    force: bool,
) -> list[int]:
    queryset = Invoice.objects.all()
    if invoice_ids is not None:
        queryset = queryset.filter(pk__in=invoice_ids)
        if force:
            queryset = queryset.filter(
                Q(status=Invoice.STATUS_PENDING)
                | Q(
                    status=Invoice.STATUS_MANUAL_REVIEW,
                    paid_at__isnull=True,
                    refund_review_required=False,
                )
            )
        else:
            queryset = queryset.filter(status=Invoice.STATUS_PENDING)
    else:
        queryset = queryset.filter(status=Invoice.STATUS_PENDING).filter(
            Q(
                checkout_state__in=(
                    Invoice.CHECKOUT_INTENT_CREATED,
                    Invoice.CHECKOUT_PROVIDER_PENDING,
                    Invoice.CHECKOUT_PROVIDER_CREATED,
                ),
            )
            | Q(
                checkout_state=Invoice.CHECKOUT_LEGACY,
                yookassa_payment_id__gt='',
            ),
        )
        if not force:
            now = timezone.now()
            queryset = queryset.filter(
                Q(next_reconciliation_at__isnull=True)
                | Q(next_reconciliation_at__lte=now),
            )
    return list(queryset.order_by('created_at').values_list('pk', flat=True)[:limit])


def _claim_provider_invoice(
    invoice_id: int,
    *,
    force: bool,
) -> tuple[Invoice, str]:
    """Claims a provider-state poll by moving next_reconciliation_at forward."""
    now = timezone.now()
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().select_related('tenant').get(
            pk=invoice_id,
        )
        recovering_manual_checkout = (
            force and _is_unsettled_checkout_manual(invoice)
        )
        if (
            invoice.status != Invoice.STATUS_PENDING
            and not recovering_manual_checkout
        ):
            return invoice, 'final'
        if not invoice.yookassa_payment_id:
            return invoice, 'missing_payment_id'
        if (
            not force
            and invoice.next_reconciliation_at is not None
            and invoice.next_reconciliation_at > now
        ):
            return invoice, 'not_due'
        if (
            invoice.reconciliation_attempts
            >= settings.YOOKASSA_RECONCILIATION_MAX_ATTEMPTS
            and not recovering_manual_checkout
        ):
            reason = 'Исчерпан лимит автоматической сверки состояния платежа YooKassa.'
            BillingService._mark_invoice_manual_review_locked(invoice, reason)
            return invoice, 'exhausted'

        invoice.last_reconciliation_at = now
        if recovering_manual_checkout:
            # Explicit operator recovery gets one authoritative read without
            # weakening the automatic retry ceiling or scheduling a sweep loop.
            invoice.next_reconciliation_at = None
            update_fields = [
                'last_reconciliation_at', 'next_reconciliation_at', 'updated_at',
            ]
        else:
            invoice.reconciliation_attempts += 1
            invoice.next_reconciliation_at = now + timedelta(
                seconds=reconciliation_delay_seconds(
                    invoice.reconciliation_attempts,
                ),
            )
            update_fields = [
                'reconciliation_attempts', 'last_reconciliation_at',
                'next_reconciliation_at', 'updated_at',
            ]
        invoice.save(update_fields=update_fields)
        return invoice, 'claimed'


def _record_invoice_poll_error(invoice_id: int, message: str) -> None:
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
        if (
            invoice.status != Invoice.STATUS_PENDING
            and not _is_unsettled_checkout_manual(invoice)
        ):
            return
        invoice.checkout_last_error = message[:500]
        invoice.save(update_fields=['checkout_last_error', 'updated_at'])


def _process_payment_state(invoice: Invoice, stats: ReconciliationStats) -> None:
    try:
        snapshot = fetch_payment(invoice.yookassa_payment_id)
    except (YooKassaAPIError, YooKassaSnapshotError) as exc:
        _record_invoice_poll_error(
            invoice.pk,
            f'Не удалось сверить платёж YooKassa: {type(exc).__name__}.',
        )
        stats.invoices_waiting += 1
        return
    except Exception as exc:
        logger.exception('Ошибка сверки YooKassa invoice=%s.', invoice.pk)
        _record_invoice_poll_error(
            invoice.pk,
            f'Неожиданная ошибка сверки YooKassa: {type(exc).__name__}.',
        )
        stats.errors += 1
        return

    if snapshot.amount != invoice.amount or snapshot.currency != invoice.currency:
        BillingService.mark_invoice_manual_review(
            invoice.pk,
            (
                'Сверка YooKassa обнаружила несовпадение суммы или валюты: '
                f'ожидалось {invoice.amount} {invoice.currency}, '
                f'получено {snapshot.amount} {snapshot.currency}.'
            ),
        )
        stats.manual_review += 1
        return
    if snapshot.test and not settings.YOOKASSA_ALLOW_TEST_PAYMENTS:
        BillingService.mark_invoice_manual_review(
            invoice.pk,
            'Тестовый платёж YooKassa не может активировать entitlement.',
        )
        stats.manual_review += 1
        return
    if snapshot.status in {'pending', 'waiting_for_capture'}:
        _record_invoice_poll_error(
            invoice.pk,
            f'Платёж YooKassa пока находится в статусе {snapshot.status}.',
        )
        stats.invoices_waiting += 1
        return

    if _is_unsettled_checkout_manual(invoice):
        # This branch is reachable only through an explicit targeted --force
        # reconciliation. The provider snapshot has been authenticated and
        # amount/currency/test flags were revalidated above, so it can safely
        # close an unpaid manual checkout without replaying a finalized event.
        if snapshot.status == 'succeeded':
            resolved = BillingService.handle_payment_success_webhook(
                invoice.pk,
                payment_id=snapshot.id,
                amount=snapshot.amount,
                currency=snapshot.currency,
            )
        elif snapshot.status == 'canceled':
            resolved = BillingService.handle_payment_failed_webhook(
                invoice.pk,
                payment_id=snapshot.id,
            )
        else:
            resolved = False
        if resolved:
            stats.invoices_final += 1
        else:
            stats.manual_review += 1
        return

    event_type = f'payment.{snapshot.status}'
    webhook_event, token, claim_state = claim_webhook_event(
        event=event_type,
        object_id=snapshot.id,
        safe_payload={
            'type': 'reconciliation',
            'event': event_type,
            'object': {
                'id': snapshot.id,
                'status': snapshot.status,
                'amount': {
                    'value': str(snapshot.amount),
                    'currency': snapshot.currency,
                },
            },
        },
        source_ip=None,
    )
    if claim_state == 'final':
        invoice.refresh_from_db(fields=['status'])
        if invoice.status == Invoice.STATUS_PENDING:
            BillingService.mark_invoice_manual_review(
                invoice.pk,
                (
                    'Авторитетное событие YooKassa уже завершено, '
                    'но Invoice остался pending.'
                ),
            )
            stats.manual_review += 1
        else:
            stats.invoices_final += 1
        return
    if claim_state == 'busy' or token is None:
        stats.invoices_waiting += 1
        return

    result = process_claimed_yookassa_event(webhook_event.pk, token)
    if not result.acknowledged:
        stats.invoices_waiting += 1
        return
    invoice.refresh_from_db(fields=['status'])
    if invoice.status == Invoice.STATUS_PENDING:
        BillingService.mark_invoice_manual_review(
            invoice.pk,
            'Авторитетное событие обработано, но Invoice остался pending.',
        )
        stats.manual_review += 1
    else:
        stats.invoices_final += 1


def _reconcile_invoices(
    stats: ReconciliationStats,
    *,
    limit: int,
    invoice_ids: list[int] | None,
    force: bool,
) -> None:
    for invoice_id in _invoice_candidate_ids(
        limit=limit,
        invoice_ids=invoice_ids,
        force=force,
    ):
        try:
            invoice = Invoice.objects.only(
                'pk', 'yookassa_payment_id', 'status', 'checkout_state',
                'paid_at', 'refund_review_required',
            ).get(pk=invoice_id)
        except Invoice.DoesNotExist:
            continue
        recovering_manual_checkout = (
            force and _is_unsettled_checkout_manual(invoice)
        )
        if (
            invoice.status != Invoice.STATUS_PENDING
            and not recovering_manual_checkout
        ):
            stats.invoices_final += 1
            continue

        if not invoice.yookassa_payment_id:
            try:
                BillingService._resume_checkout_intent(
                    invoice.pk,
                    respect_backoff=not force,
                )
            except CheckoutPendingError:
                stats.invoices_waiting += 1
            except CheckoutManualReviewError:
                stats.manual_review += 1
            except CheckoutTerminalError:
                # A webhook may finalize the Invoice between candidate
                # selection and the row lock in _resume_checkout_intent.
                stats.invoices_final += 1
            except Exception:
                logger.exception('Ошибка возобновления checkout invoice=%s.', invoice.pk)
                stats.errors += 1
            else:
                stats.invoices_resumed += 1
            continue

        try:
            invoice, claim_state = _claim_provider_invoice(
                invoice.pk,
                force=force,
            )
        except Invoice.DoesNotExist:
            continue
        if claim_state == 'final':
            stats.invoices_final += 1
            continue
        if claim_state == 'exhausted':
            stats.manual_review += 1
            continue
        if claim_state != 'claimed':
            continue
        stats.invoices_claimed += 1
        _process_payment_state(invoice, stats)


def reconcile_yookassa_billing(
    *,
    limit: int | None = None,
    force: bool = False,
    event_ids: list[int] | None = None,
    invoice_ids: list[int] | None = None,
) -> dict[str, int]:
    """Durably reconciles failed events and incomplete checkout/payment intents."""
    batch_limit = min(
        max(1, int(limit or settings.YOOKASSA_RECONCILIATION_BATCH_SIZE)),
        1000,
    )
    stats = ReconciliationStats()
    _reconcile_events(
        stats,
        limit=batch_limit,
        event_ids=event_ids,
        force=force,
    )
    _reconcile_invoices(
        stats,
        limit=batch_limit,
        invoice_ids=invoice_ids,
        force=force,
    )
    result = stats.to_dict()
    logger.info('YooKassa reconciliation: %s', result)
    return result
