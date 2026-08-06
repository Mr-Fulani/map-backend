from django.urls import path

from apps.web_research.views import (
    ListingMarketComparisonView, ProductMarketOfferListView,
    ProductMarketResearchView, TenantWebResearchSettingsView,
    ProductWebResearchView, WebResearchRunDetailView, WebResearchRunListView,
    WebSearchProviderListView,
)


urlpatterns = [
    path(
        'web-research/settings/',
        TenantWebResearchSettingsView.as_view(),
        name='tenant-web-research-settings',
    ),
    path(
        'web-research/providers/',
        WebSearchProviderListView.as_view(),
        name='web-search-provider-list',
    ),
    path(
        'web-research/runs/',
        WebResearchRunListView.as_view(),
        name='web-research-run-list',
    ),
    path(
        'products/<int:product_pk>/web-research/',
        ProductWebResearchView.as_view(),
        name='product-web-research',
    ),
    path(
        'products/<int:product_pk>/market-research/',
        ProductMarketResearchView.as_view(),
        name='product-market-research',
    ),
    path(
        'products/<int:product_pk>/market-offers/',
        ProductMarketOfferListView.as_view(),
        name='product-market-offers',
    ),
    path(
        'listings/<int:listing_pk>/market-comparison/',
        ListingMarketComparisonView.as_view(),
        name='listing-market-comparison',
    ),
    path(
        'web-research/runs/<int:pk>/',
        WebResearchRunDetailView.as_view(),
        name='web-research-run-detail',
    ),
]
