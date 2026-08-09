from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.billing.models import Subscription
from apps.billing.views import SubscriptionView
from apps.tenants.views import MeView


class _MissingSubscriptionTenant:
    pk = 1
    slug = 'missing-subscription'
    name = 'Missing subscription'

    @property
    def subscription(self):
        raise Subscription.DoesNotExist


class _BrokenSubscriptionTenant(_MissingSubscriptionTenant):
    @property
    def subscription(self):
        raise RuntimeError('database is unavailable')


def test_subscription_view_returns_none_only_when_subscription_is_missing():
    response = SubscriptionView().get(
        SimpleNamespace(tenant=_MissingSubscriptionTenant()),
    )

    assert response.data == {'status': 'ok', 'data': None}

    with pytest.raises(RuntimeError, match='database is unavailable'):
        SubscriptionView().get(
            SimpleNamespace(tenant=_BrokenSubscriptionTenant()),
        )


@patch('apps.tenants.models.TenantUser.objects.filter')
def test_me_view_does_not_mask_subscription_backend_failures(membership_filter):
    membership_filter.return_value.first.return_value = None
    request = SimpleNamespace(
        user=SimpleNamespace(pk=7, email='owner@example.test', phone=''),
        tenant=_BrokenSubscriptionTenant(),
    )

    with pytest.raises(RuntimeError, match='database is unavailable'):
        MeView().get(request)
