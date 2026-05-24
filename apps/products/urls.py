from django.urls import path

from apps.products.views import (
    ProductDetailView, ProductListView,
    ProductPublishView, ProductRegenerateView, ProductSyncView,
)

urlpatterns = [
    path('', ProductListView.as_view(), name='product-list'),
    path('<int:pk>/', ProductDetailView.as_view(), name='product-detail'),
    path('<int:pk>/publish/', ProductPublishView.as_view(), name='product-publish'),
    path('<int:pk>/regenerate/', ProductRegenerateView.as_view(), name='product-regenerate'),
    path('sync/<int:connection_id>/', ProductSyncView.as_view(), name='product-sync'),
]
