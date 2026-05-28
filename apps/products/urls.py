from django.urls import path

from apps.products.views import (
    ProductArchiveView, ProductBulkActionDetailView, ProductBulkActionView,
    ProductCrossCodesView, ProductDetailView, ProductFitmentsView,
    ProductListView, ProductParseJobDetailView, ProductParseView,
    ProductPublishView, ProductRegenerateView, ProductSearchView,
    ProductSyncView,
)

urlpatterns = [
    path('', ProductListView.as_view(), name='product-list'),
    path('search/', ProductSearchView.as_view(), name='product-search'),
    path('parse/', ProductParseView.as_view(), name='product-parse'),
    path('parse-jobs/<int:pk>/', ProductParseJobDetailView.as_view(), name='product-parse-job-detail'),
    path('bulk-actions/', ProductBulkActionView.as_view(), name='product-bulk-action'),
    path('bulk-actions/<int:pk>/', ProductBulkActionDetailView.as_view(), name='product-bulk-action-detail'),
    path('sync/<int:connection_id>/', ProductSyncView.as_view(), name='product-sync'),
    path('<int:pk>/', ProductDetailView.as_view(), name='product-detail'),
    path('<int:pk>/fitments/', ProductFitmentsView.as_view(), name='product-fitments'),
    path('<int:pk>/cross-codes/', ProductCrossCodesView.as_view(), name='product-cross-codes'),
    path('<int:pk>/publish/', ProductPublishView.as_view(), name='product-publish'),
    path('<int:pk>/archive/', ProductArchiveView.as_view(), name='product-archive'),
    path('<int:pk>/regenerate/', ProductRegenerateView.as_view(), name='product-regenerate'),
]
