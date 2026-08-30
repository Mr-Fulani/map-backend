from django.urls import path

from apps.marketplaces.views import (
    AutoloadStatusView,
    MarketplaceAccountDetailView,
    MarketplaceAccountListView,
    MarketplaceProviderRolloutView,
    MarketplacePlacementAddressDetailView,
    MarketplacePlacementAddressListView,
)
from apps.marketplaces.ozon_catalog_views import OzonCatalogTypesView, OzonCatalogView

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
    path(
        '<int:pk>/ozon-catalog/',
        OzonCatalogView.as_view(),
        name='account-ozon-catalog',
    ),
    path(
        '<int:pk>/ozon-catalog/types/',
        OzonCatalogTypesView.as_view(),
        name='account-ozon-catalog-types',
    ),
    path('<int:pk>/autoload-status/', AutoloadStatusView.as_view(), name='account-autoload-status'),
]
