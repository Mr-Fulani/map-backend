from django.urls import path

from apps.marketplaces.views import (
    AutoloadStatusView,
    MarketplaceAccountDetailView,
    MarketplaceAccountListView,
    MarketplaceProviderRolloutView,
    MarketplacePlacementAddressDetailView,
    MarketplacePlacementAddressListView,
)

urlpatterns = [
    path('', MarketplaceAccountListView.as_view(), name='account-list'),
    path(
        'provider-rollout/',
        MarketplaceProviderRolloutView.as_view(),
        name='account-provider-rollout',
    ),
    path('placement-addresses/', MarketplacePlacementAddressListView.as_view(), name='placement-address-list'),
    path(
        'placement-addresses/<int:pk>/',
        MarketplacePlacementAddressDetailView.as_view(),
        name='placement-address-detail',
    ),
    path('<int:pk>/', MarketplaceAccountDetailView.as_view(), name='account-detail'),
    path('<int:pk>/autoload-status/', AutoloadStatusView.as_view(), name='account-autoload-status'),
]
