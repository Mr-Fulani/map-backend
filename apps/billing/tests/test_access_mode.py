from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.test import Client

from apps.billing.models import Subscription
from apps.tenants.services import TenantService


@pytest.mark.django_db
def test_expired_subscription_allows_reads_but_blocks_mutations():
    tenant, api_key = TenantService.create_tenant(
        'Read Only', 'read-only-co', 'read-only@test.com', 'pass12345',
    )
    sub = tenant.subscription
    sub.current_period_end = date.today() - timedelta(days=1)
    sub.save(update_fields=['current_period_end'])
    auth = {'HTTP_AUTHORIZATION': f'Bearer {api_key}'}

    read_response = Client().get('/api/v1/billing/subscription/', **auth)
    write_response = Client().post(
        '/api/v1/products/catalog-categories/',
        {'name': 'Новая категория'},
        content_type='application/json',
        **auth,
    )

    assert read_response.status_code == 200
    assert read_response.json()['data']['status'] == Subscription.STATUS_TRIAL
    assert read_response.json()['data']['effective_status'] == Subscription.STATUS_PAST_DUE
    assert read_response.json()['data']['access_mode'] == Subscription.ACCESS_BILLING_ONLY
    assert write_response.status_code == 402
    assert write_response.json()['code'] == 'subscription_inactive'


@pytest.mark.django_db
def test_expired_subscription_can_open_checkout():
    tenant, api_key = TenantService.create_tenant(
        'Renew', 'renew-co', 'renew@test.com', 'pass12345',
    )
    sub = tenant.subscription
    sub.current_period_end = date.today() - timedelta(days=1)
    sub.save(update_fields=['current_period_end'])

    with patch(
        'apps.billing.services.BillingService.create_payment',
        return_value='https://payments.example/renew',
    ) as create_payment:
        response = Client().post(
            '/api/v1/billing/checkout/',
            {
                'plan_slug': sub.plan.slug,
                'period': Subscription.PERIOD_MONTHLY,
                'return_url': 'https://app.example/billing',
            },
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {api_key}',
        )

    assert response.status_code == 200
    assert response.json()['data']['payment_url'] == 'https://payments.example/renew'
    create_payment.assert_called_once()
