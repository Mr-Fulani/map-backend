from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command, CommandError
from django.db import transaction
from django.utils import timezone
from django_celery_beat.models import PeriodicTask

from apps.billing.models import BillingOutboxEvent, Invoice, Subscription
from apps.billing.outbox import (
    BillingOutboxConflictError, dispatch_due_billing_outbox,
    enqueue_limit_reached_requeue,
    enqueue_notification,
)
from apps.billing.services import BillingService
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(
        slug,
        slug,
        f'{slug}@test.com',
        'pass12345',
    )
    return tenant


@pytest.mark.django_db(transaction=True)
def test_broker_failure_after_financial_commit_keeps_outbox_pending():
    tenant = make_tenant('outbox-financial-commit')
    invoice = Invoice.objects.create(
        tenant=tenant,
        amount=Decimal('100.00'),
        currency='RUB',
        status=Invoice.STATUS_PENDING,
        yookassa_payment_id='pay_outbox_financial_commit',
        metadata={},
    )

    with patch(
        'apps.billing.tasks.dispatch_billing_outbox.delay',
        side_effect=RuntimeError('broker unavailable'),
    ) as broker_publish:
        assert BillingService.handle_payment_success_webhook(
            invoice.pk,
            payment_id=invoice.yookassa_payment_id,
            amount=invoice.amount,
            currency=invoice.currency,
        ) is True

    invoice.refresh_from_db()
    assert invoice.status == Invoice.STATUS_PAID
    assert broker_publish.call_count == 2
    events = BillingOutboxEvent.objects.filter(invoice=invoice)
    assert events.count() == 2
    assert set(events.values_list('status', flat=True)) == {
        BillingOutboxEvent.STATUS_PENDING,
    }


@pytest.mark.django_db
def test_outbox_row_rolls_back_with_financial_transaction():
    tenant = make_tenant('outbox-rollback')

    with pytest.raises(RuntimeError, match='rollback'), transaction.atomic():
        enqueue_notification(
            tenant=tenant,
            level='billing',
            message='Must roll back',
            idempotency_key='rollback-notification:v1',
        )
        raise RuntimeError('rollback')

    assert not BillingOutboxEvent.objects.filter(tenant=tenant).exists()


@pytest.mark.django_db
def test_outbox_idempotency_key_cannot_change_payload():
    tenant = make_tenant('outbox-idempotency-conflict')
    with patch(
        'apps.billing.outbox._kick_dispatcher_safely',
    ), transaction.atomic():
        first = enqueue_notification(
            tenant=tenant,
            level='billing',
            message='Original',
            idempotency_key='immutable-notification:v1',
        )
        repeated = enqueue_notification(
            tenant=tenant,
            level='billing',
            message='Original',
            idempotency_key='immutable-notification:v1',
        )
        with pytest.raises(BillingOutboxConflictError):
            enqueue_notification(
                tenant=tenant,
                level='billing',
                message='Changed',
                idempotency_key='immutable-notification:v1',
            )

    assert repeated.pk == first.pk
    assert BillingOutboxEvent.objects.filter(tenant=tenant).count() == 1


@pytest.mark.django_db
def test_notification_publish_failure_is_rescheduled_with_backoff():
    tenant = make_tenant('outbox-notification-retry')
    with patch(
        'apps.billing.outbox._kick_dispatcher_safely',
    ), transaction.atomic():
        event = enqueue_notification(
            tenant=tenant,
            level='billing',
            message='Payment succeeded',
            idempotency_key='notification-retry:v1',
        )

    before = timezone.now()
    with patch(
        'apps.notifications.tasks.send_notification_task.apply_async',
        side_effect=RuntimeError('broker unavailable'),
    ):
        stats = dispatch_due_billing_outbox(event_ids=[event.pk])

    event.refresh_from_db()
    assert stats['claimed'] == 1
    assert stats['retryable'] == 1
    assert stats['errors'] == 1
    assert event.status == BillingOutboxEvent.STATUS_PENDING
    assert event.attempts == 1
    assert event.next_attempt_at >= before
    assert event.processing_token is None
    assert 'RuntimeError' in event.last_error


@pytest.mark.django_db
def test_poison_outbox_event_is_dead_lettered_and_can_be_force_recovered(settings):
    settings.BILLING_OUTBOX_MAX_ATTEMPTS = 1
    tenant = make_tenant('outbox-dead-letter')
    with patch(
        'apps.billing.outbox._kick_dispatcher_safely',
    ), transaction.atomic():
        event = enqueue_notification(
            tenant=tenant,
            level='billing',
            message='Needs operator recovery',
            idempotency_key='dead-letter:v1',
        )

    with patch(
        'apps.notifications.tasks.send_notification_task.apply_async',
        side_effect=RuntimeError('broker rejects payload'),
    ):
        stats = dispatch_due_billing_outbox(event_ids=[event.pk])

    event.refresh_from_db()
    assert stats['dead_lettered'] == 1
    assert stats['retryable'] == 0
    assert event.status == BillingOutboxEvent.STATUS_DEAD
    assert event.dead_lettered_at is not None
    assert event.next_attempt_at is None

    with patch(
        'apps.notifications.tasks.send_notification_task.apply_async',
    ) as publish:
        skipped = dispatch_due_billing_outbox(event_ids=[event.pk])
        recovered = dispatch_due_billing_outbox(event_ids=[event.pk], force=True)

    event.refresh_from_db()
    assert skipped['claimed'] == 0
    assert recovered['dispatched'] == 1
    assert event.status == BillingOutboxEvent.STATUS_DISPATCHED
    assert event.dead_lettered_at is None
    publish.assert_called_once()


def test_force_dispatch_requires_explicit_event_ids():
    with pytest.raises(ValueError, match='event_ids'):
        dispatch_due_billing_outbox(force=True)


def test_force_command_requires_explicit_event_id():
    with pytest.raises(CommandError, match='--event-id'):
        call_command('dispatch_billing_outbox', '--force')


@pytest.mark.django_db
def test_successful_publish_marks_event_dispatched_with_stable_task_id():
    tenant = make_tenant('outbox-notification-success')
    with patch(
        'apps.billing.outbox._kick_dispatcher_safely',
    ), transaction.atomic():
        event = enqueue_notification(
            tenant=tenant,
            level='billing',
            message='Payment succeeded',
            idempotency_key='notification-success:v1',
        )

    with patch(
        'apps.notifications.tasks.send_notification_task.apply_async',
    ) as publish:
        stats = dispatch_due_billing_outbox(event_ids=[event.pk])

    event.refresh_from_db()
    assert stats['dispatched'] == 1
    assert event.status == BillingOutboxEvent.STATUS_DISPATCHED
    assert event.dispatched_at is not None
    publish.assert_called_once_with(
        args=[tenant.pk, 'billing', 'Payment succeeded', {}],
        task_id=f'billing-outbox-{event.pk}',
    )


@pytest.mark.django_db
def test_targeted_command_does_not_dispatch_unrelated_outbox_event():
    tenant = make_tenant('outbox-command-target')
    with patch(
        'apps.billing.outbox._kick_dispatcher_safely',
    ), transaction.atomic():
        selected = enqueue_notification(
            tenant=tenant,
            level='billing',
            message='Selected',
            idempotency_key='command-selected:v1',
        )
        unrelated = enqueue_limit_reached_requeue(
            tenant=tenant,
            idempotency_key='command-unrelated:v1',
        )

    stdout = StringIO()
    with patch(
        'apps.notifications.tasks.send_notification_task.apply_async',
    ) as notification_publish, patch(
        'apps.marketplaces.tasks.requeue_limit_reached_listings.apply_async',
    ) as requeue_publish:
        call_command(
            'dispatch_billing_outbox',
            '--event-id',
            str(selected.pk),
            '--force',
            stdout=stdout,
        )

    selected.refresh_from_db()
    unrelated.refresh_from_db()
    assert selected.status == BillingOutboxEvent.STATUS_DISPATCHED
    assert unrelated.status == BillingOutboxEvent.STATUS_PENDING
    notification_publish.assert_called_once()
    requeue_publish.assert_not_called()
    assert '"dispatched": 1' in stdout.getvalue()


@pytest.mark.django_db
def test_periodic_setup_registers_billing_outbox_dispatcher():
    call_command('setup_periodic_tasks', stdout=StringIO())

    task = PeriodicTask.objects.get(name='dispatch_billing_outbox')
    assert task.task == 'apps.billing.tasks.dispatch_billing_outbox'
    assert task.queue == 'billing'
    assert task.interval.every == 1
    assert task.interval.period == 'minutes'


@pytest.mark.django_db
def test_stale_processing_lease_is_recovered_but_fresh_lease_is_not(settings):
    settings.BILLING_OUTBOX_PROCESSING_TIMEOUT_SECONDS = 60
    tenant = make_tenant('outbox-processing-lease')
    stale = BillingOutboxEvent.objects.create(
        tenant=tenant,
        event_type=BillingOutboxEvent.EVENT_REQUEUE_LIMIT_REACHED,
        idempotency_key='stale-lease:v1',
        payload={'schema': 1},
        status=BillingOutboxEvent.STATUS_PROCESSING,
        processing_started_at=timezone.now() - timedelta(seconds=61),
    )
    fresh = BillingOutboxEvent.objects.create(
        tenant=tenant,
        event_type=BillingOutboxEvent.EVENT_REQUEUE_LIMIT_REACHED,
        idempotency_key='fresh-lease:v1',
        payload={'schema': 1},
        status=BillingOutboxEvent.STATUS_PROCESSING,
        processing_started_at=timezone.now(),
    )

    with patch(
        'apps.marketplaces.tasks.requeue_limit_reached_listings.apply_async',
    ) as publish:
        stats = dispatch_due_billing_outbox()

    stale.refresh_from_db()
    fresh.refresh_from_db()
    assert stats['dispatched'] == 1
    assert stale.status == BillingOutboxEvent.STATUS_DISPATCHED
    assert fresh.status == BillingOutboxEvent.STATUS_PROCESSING
    publish.assert_called_once()


@pytest.mark.django_db
def test_expiration_transition_and_notification_outbox_are_atomic():
    tenant = make_tenant('outbox-expiration')
    subscription = tenant.subscription
    subscription.current_period_end = timezone.localdate() - timedelta(days=1)
    subscription.save(update_fields=['current_period_end'])

    assert BillingService.check_expired_trials() == 1

    subscription.refresh_from_db()
    event = BillingOutboxEvent.objects.get(
        tenant=tenant,
        event_type=BillingOutboxEvent.EVENT_NOTIFICATION,
    )
    assert subscription.status == Subscription.STATUS_PAST_DUE
    assert event.payload['level'] == 'billing'
