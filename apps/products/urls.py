from django.urls import path

from apps.products.views import ProductDetailView, ProductListView, ProductSyncView

urlpatterns = [
    path('', ProductListView.as_view(), name='product-list'),
    path('<int:pk>/', ProductDetailView.as_view(), name='product-detail'),
    path('sync/<int:connection_id>/', ProductSyncView.as_view(), name='product-sync'),
]
