from django.urls import path

from apps.web_research.views import (
    ProductWebResearchView, WebResearchRunDetailView, WebResearchRunListView,
    WebSearchProviderListView,
)


urlpatterns = [
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
        'web-research/runs/<int:pk>/',
        WebResearchRunDetailView.as_view(),
        name='web-research-run-detail',
    ),
]
