from datetime import timedelta

import pytest
from django.utils import timezone

from apps.billing.models import BillingOutboxEvent
from apps.core.retention import purge_retained_data
from apps.tenants.services import TenantService


@pytest.mark.django_db
def test_retention_deletes_only_expired_dispatched_billing_outbox(settings):
    settings.BILLING_AUDIT_RETENTION_DAYS = 30
    tenant, _ = TenantService.create_tenant(
        'Retention Corp',
        'retention-outbox',
        'retention@example.com',
        'pass12345',
    )
    old = timezone.now() - timedelta(days=31)
    fresh = timezone.now() - timedelta(days=29)

    expired = BillingOutboxEvent.objects.create(
        tenant=tenant,
        event_type=BillingOutboxEvent.EVENT_NOTIFICATION,
        idempotency_key='expired:v1',
        payload={'schema': 1},
        status=BillingOutboxEvent.STATUS_DISPATCHED,
        dispatched_at=old,
    )
    retained = BillingOutboxEvent.objects.create(
        tenant=tenant,
        event_type=BillingOutboxEvent.EVENT_NOTIFICATION,
        idempotency_key='fresh:v1',
        payload={'schema': 1},
        status=BillingOutboxEvent.STATUS_DISPATCHED,
        dispatched_at=fresh,
    )
    pending = BillingOutboxEvent.objects.create(
        tenant=tenant,
        event_type=BillingOutboxEvent.EVENT_NOTIFICATION,
        idempotency_key='pending:v1',
        payload={'schema': 1},
        status=BillingOutboxEvent.STATUS_PENDING,
        dispatched_at=old,
    )

    result = purge_retained_data()

    assert result['billing_outbox_events'] == 1
    assert not BillingOutboxEvent.objects.filter(pk=expired.pk).exists()
    assert BillingOutboxEvent.objects.filter(pk=retained.pk).exists()
    assert BillingOutboxEvent.objects.filter(pk=pending.pk).exists()
