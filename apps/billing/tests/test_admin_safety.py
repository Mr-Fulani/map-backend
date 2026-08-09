import pytest
from django.contrib.admin.sites import AdminSite

from apps.billing.admin import AIWalletAdmin, InvoiceAdmin, SubscriptionAdmin
from apps.billing.models import AIWallet, Invoice, Subscription


@pytest.mark.parametrize(
    ('admin_class', 'model'),
    [
        (SubscriptionAdmin, Subscription),
        (InvoiceAdmin, Invoice),
        (AIWalletAdmin, AIWallet),
    ],
)
def test_system_financial_admins_are_strictly_read_only(admin_class, model):
    model_admin = admin_class(model, AdminSite())

    assert model_admin.has_add_permission(request=None) is False
    assert model_admin.has_change_permission(request=None) is False
    assert model_admin.has_delete_permission(request=None) is False
