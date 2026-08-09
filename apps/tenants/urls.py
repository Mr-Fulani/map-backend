from django.urls import path
from apps.tenants.views import (
    APIKeyListView,
    APIKeyRevokeView,
    CatalogDomainListView,
    MeView,
    RegisterView,
    TenantDetailView,
    TenantUserListView,
    WebhookEndpointDetailView,
    WebhookDeliveryListView,
    WebhookEndpointListView,
    WebhookEndpointTestView,
    WebhookEventsView,
)
from apps.tenants.jwt_views import (
    BrowserCSRFView,
    BrowserLoginView,
    BrowserLogoutAllView,
    BrowserLogoutView,
    BrowserRefreshView,
    LogoutAllView,
    LogoutView,
    TenantTokenObtainPairView,
    TenantTokenRefreshView,
)

urlpatterns = [
    # Auth
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/token/', TenantTokenObtainPairView.as_view(), name='token-obtain'),
    path('auth/token/refresh/', TenantTokenRefreshView.as_view(), name='token-refresh'),
    path('auth/logout/', LogoutView.as_view(), name='logout'),
    path('auth/logout-all/', LogoutAllView.as_view(), name='logout-all'),
    path('auth/browser/csrf/', BrowserCSRFView.as_view(), name='browser-csrf'),
    path('auth/browser/login/', BrowserLoginView.as_view(), name='browser-login'),
    path('auth/browser/refresh/', BrowserRefreshView.as_view(), name='browser-refresh'),
    path('auth/browser/logout/', BrowserLogoutView.as_view(), name='browser-logout'),
    path(
        'auth/browser/logout-all/',
        BrowserLogoutAllView.as_view(),
        name='browser-logout-all',
    ),
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
    path('webhooks/deliveries/', WebhookDeliveryListView.as_view(), name='webhook-deliveries'),
    path('webhooks/<int:pk>/', WebhookEndpointDetailView.as_view(), name='webhook-detail'),
    path('webhooks/<int:pk>/test/', WebhookEndpointTestView.as_view(), name='webhook-test'),
]
