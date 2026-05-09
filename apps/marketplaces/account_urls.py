from django.urls import path

from apps.marketplaces.views import MarketplaceAccountDetailView, MarketplaceAccountListView

urlpatterns = [
    path('', MarketplaceAccountListView.as_view(), name='account-list'),
    path('<int:pk>/', MarketplaceAccountDetailView.as_view(), name='account-detail'),
]
