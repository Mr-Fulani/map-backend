from django.urls import path

from apps.marketplaces.views import (
    ListingApproveView,
    ListingDetailView,
    ListingListView,
    ListingRegenerateView,
)

urlpatterns = [
    path('', ListingListView.as_view(), name='listing-list'),
    path('<int:pk>/', ListingDetailView.as_view(), name='listing-detail'),
    path('<int:pk>/approve/', ListingApproveView.as_view(), name='listing-approve'),
    path('<int:pk>/regenerate/', ListingRegenerateView.as_view(), name='listing-regenerate'),
]
