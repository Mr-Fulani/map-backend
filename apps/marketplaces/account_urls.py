from django.urls import path

from apps.marketplaces.views import (
    AutoloadStatusView,
    MarketplaceAccountDetailView,
    MarketplaceAccountListView,
)

urlpatterns = [
    path('', MarketplaceAccountListView.as_view(), name='account-list'),
    path('<int:pk>/', MarketplaceAccountDetailView.as_view(), name='account-detail'),
    path('<int:pk>/autoload-status/', AutoloadStatusView.as_view(), name='account-autoload-status'),
]
