import pytest
from django.contrib.admin.sites import AdminSite

from apps.ai_agent.admin import AIProviderOperationAdmin, AIRequestLogAdmin
from apps.ai_agent.models import AIProviderOperation
from apps.billing.admin import AIWalletAdmin, InvoiceAdmin, SubscriptionAdmin
from apps.billing.models import AIWallet, Invoice, Subscription
from apps.core.admin import (
    BackgroundJobDispatchAdmin, SuperuserReadOnlyAdminMixin,
    TenantScopedAdminMixin, TenantScopedReadOnlyAdminMixin,
)
from apps.image_search.admin import ImageSearchLogAdmin, ImageSearchTaskAdmin
from apps.image_search.admin import ImageSearchCacheAdmin
from apps.media_processing.admin import MediaProcessingJobAdmin
from apps.notifications.admin import (
    NotificationDeliveryAdmin, TenantNotificationSettingsAdmin,
)


@pytest.mark.parametrize(
    ('admin_class', 'model'),
    [
        (SubscriptionAdmin, Subscription),
        (InvoiceAdmin, Invoice),
        (AIWalletAdmin, AIWallet),
        (AIProviderOperationAdmin, AIProviderOperation),
    ],
)
def test_system_financial_admins_are_strictly_read_only(admin_class, model):
    model_admin = admin_class(model, AdminSite())

    assert model_admin.has_add_permission(request=None) is False
    assert model_admin.has_change_permission(request=None) is False
    assert model_admin.has_delete_permission(request=None) is False


@pytest.mark.parametrize(
    'admin_class',
    [
        AIProviderOperationAdmin,
        AIRequestLogAdmin,
        NotificationDeliveryAdmin,
        ImageSearchLogAdmin,
        ImageSearchTaskAdmin,
    ],
)
def test_tenant_owned_provider_journals_use_membership_scope(admin_class):
    assert issubclass(admin_class, TenantScopedReadOnlyAdminMixin)


def test_media_provider_journal_uses_membership_scope():
    assert issubclass(MediaProcessingJobAdmin, TenantScopedAdminMixin)


def test_editable_notification_settings_use_membership_scope():
    assert issubclass(TenantNotificationSettingsAdmin, TenantScopedAdminMixin)


def test_global_background_dispatch_journal_is_superuser_only():
    assert issubclass(BackgroundJobDispatchAdmin, SuperuserReadOnlyAdminMixin)
    assert issubclass(ImageSearchCacheAdmin, SuperuserReadOnlyAdminMixin)
