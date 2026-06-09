from django.urls import path

from apps.marketplaces.views import (
    ListingApproveView,
    ListingArchiveView,
    ListingBulkActionView,
    ListingBulkPlacementView,
    ListingCheckStatusView,
    ListingDeleteView,
    ListingDetailView,
    ListingListView,
    ListingPublishView,
    ListingRegenerateView,
)

urlpatterns = [
    path('', ListingListView.as_view(), name='listing-list'),
    path('bulk-actions/', ListingBulkActionView.as_view(), name='listing-bulk-actions'),
    path('bulk-placement/', ListingBulkPlacementView.as_view(), name='listing-bulk-placement'),
    path('<int:pk>/', ListingDetailView.as_view(), name='listing-detail'),
    path('<int:pk>/approve/', ListingApproveView.as_view(), name='listing-approve'),
    path('<int:pk>/publish/', ListingPublishView.as_view(), name='listing-publish'),
    path('<int:pk>/archive/', ListingArchiveView.as_view(), name='listing-archive'),
    path('<int:pk>/delete/', ListingDeleteView.as_view(), name='listing-delete'),
    path('<int:pk>/check-status/', ListingCheckStatusView.as_view(), name='listing-check-status'),
    path('<int:pk>/regenerate/', ListingRegenerateView.as_view(), name='listing-regenerate'),
]
