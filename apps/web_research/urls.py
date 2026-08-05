from django.urls import path

from apps.web_research.views import ProductWebResearchView, WebResearchRunDetailView


urlpatterns = [
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
