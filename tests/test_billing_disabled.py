import uuid
from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.billing.reconciliation import reconcile_yookassa_billing
from apps.billing.services import BillingDisabledError, BillingService
from apps.billing.views import (
    AITopupCheckoutView, CheckoutView, YooKassaWebhookView,
)
from apps.billing.yookassa_client import YooKassaAPIError, fetch_payment


@pytest.mark.parametrize(
    'view_class',
    (CheckoutView, AITopupCheckoutView, YooKassaWebhookView),
)
@override_settings(BILLING_ENABLED=False)
def test_provider_backed_http_mutations_fail_closed(view_class):
    response = view_class().post(object())

    assert response.status_code == 503
    assert response.data == {
        'status': 'error',
        'code': 'billing_disabled',
        'message': 'Онлайн-оплата временно недоступна.',
    }


@override_settings(BILLING_ENABLED=False)
def test_checkout_service_stops_before_database_or_provider_work():
    with patch.object(BillingService, '_normalize_client_checkout_key') as normalize, \
         pytest.raises(BillingDisabledError):
        BillingService.create_payment(
            tenant=None,
            plan_slug='starter',
            period='monthly',
            return_url='https://app.example.test/return',
            idempotency_key=uuid.uuid4(),
        )

    normalize.assert_not_called()


@override_settings(BILLING_ENABLED=False)
def test_yookassa_client_stops_before_network_access():
    with patch('apps.billing.yookassa_client.requests.get') as request_get, \
         pytest.raises(YooKassaAPIError, match='отключён оператором'):
        fetch_payment('pay_disabled')

    request_get.assert_not_called()


@override_settings(BILLING_ENABLED=False)
def test_reconciliation_is_a_database_free_noop():
    result = reconcile_yookassa_billing()

    assert result
    assert set(result.values()) == {0}
