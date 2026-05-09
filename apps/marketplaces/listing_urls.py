from django.urls import path

from apps.marketplaces.views import ListingListView

urlpatterns = [
    path('', ListingListView.as_view(), name='listing-list'),
]
