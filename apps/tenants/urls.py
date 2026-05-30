from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from apps.tenants.views import (
    APIKeyListView,
    APIKeyRevokeView,
    CatalogDomainListView,
    MeView,
    RegisterView,
    TenantDetailView,
    TenantUserListView,
    WebhookEndpointDetailView,
    WebhookEndpointListView,
    WebhookEndpointTestView,
    WebhookEventsView,
)
from apps.tenants.jwt_views import TenantTokenObtainPairView

urlpatterns = [
    # Auth
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/token/', TenantTokenObtainPairView.as_view(), name='token-obtain'),
    path('auth/token/refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    path('auth/me/', MeView.as_view(), name='auth-me'),
    # Tenant
    path('catalog-domains/', CatalogDomainListView.as_view(), name='catalog-domain-list'),
    path('tenant/', TenantDetailView.as_view(), name='tenant-detail'),
    path('tenant/users/', TenantUserListView.as_view(), name='tenant-users'),
    path('tenant/api-keys/', APIKeyListView.as_view(), name='api-keys-list'),
    path('tenant/api-keys/<int:key_id>/', APIKeyRevokeView.as_view(), name='api-key-revoke'),
    # Webhooks
    path('webhooks/', WebhookEndpointListView.as_view(), name='webhook-list'),
    path('webhooks/events/', WebhookEventsView.as_view(), name='webhook-events'),
    path('webhooks/<int:pk>/', WebhookEndpointDetailView.as_view(), name='webhook-detail'),
    path('webhooks/<int:pk>/test/', WebhookEndpointTestView.as_view(), name='webhook-test'),
]
