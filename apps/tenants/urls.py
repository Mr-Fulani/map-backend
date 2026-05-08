from django.urls import path

from apps.tenants.views import (
    APIKeyListView,
    APIKeyRevokeView,
    RegisterView,
    TenantDetailView,
    TenantUserListView,
)

urlpatterns = [
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('tenant/', TenantDetailView.as_view(), name='tenant-detail'),
    path('tenant/users/', TenantUserListView.as_view(), name='tenant-users'),
    path('tenant/api-keys/', APIKeyListView.as_view(), name='api-keys-list'),
    path('tenant/api-keys/<int:key_id>/', APIKeyRevokeView.as_view(), name='api-key-revoke'),
]
