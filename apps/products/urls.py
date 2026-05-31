from django.urls import path

from apps.products.views import (
    ProductArchiveView, ProductBulkActionDetailView, ProductBulkActionView,
    ProductCrossCodesView, ProductDetailView, ProductFitmentsView,
    ProductCatalogCategoryAssignView, ProductListView, ProductParseJobDetailView,
    ProductParseView, ProductPublishView, ProductRegenerateView, ProductSearchView,
    ProductSyncView, TenantCatalogCategoryDetailView,
    TenantCatalogCategoryListView, TenantCategoryMappingListView,
    TenantSourceCategoryListView,
)

urlpatterns = [
    path('', ProductListView.as_view(), name='product-list'),
    path('search/', ProductSearchView.as_view(), name='product-search'),
    path('parse/', ProductParseView.as_view(), name='product-parse'),
    path('parse-jobs/<int:pk>/', ProductParseJobDetailView.as_view(), name='product-parse-job-detail'),
    path('bulk-actions/', ProductBulkActionView.as_view(), name='product-bulk-action'),
    path('bulk-actions/<int:pk>/', ProductBulkActionDetailView.as_view(), name='product-bulk-action-detail'),
    path('catalog-categories/', TenantCatalogCategoryListView.as_view(), name='tenant-catalog-category-list'),
    path(
        'catalog-categories/assign/',
        ProductCatalogCategoryAssignView.as_view(),
        name='product-catalog-category-assign',
    ),
    path(
        'catalog-categories/<int:pk>/',
        TenantCatalogCategoryDetailView.as_view(),
        name='tenant-catalog-category-detail',
    ),
    path(
        'catalog-category-mappings/',
        TenantCategoryMappingListView.as_view(),
        name='tenant-category-mapping-list',
    ),
    path(
        'catalog-source-categories/',
        TenantSourceCategoryListView.as_view(),
        name='tenant-source-category-list',
    ),
    path('sync/<int:connection_id>/', ProductSyncView.as_view(), name='product-sync'),
    path('<int:pk>/', ProductDetailView.as_view(), name='product-detail'),
    path('<int:pk>/fitments/', ProductFitmentsView.as_view(), name='product-fitments'),
    path('<int:pk>/cross-codes/', ProductCrossCodesView.as_view(), name='product-cross-codes'),
    path('<int:pk>/publish/', ProductPublishView.as_view(), name='product-publish'),
    path('<int:pk>/archive/', ProductArchiveView.as_view(), name='product-archive'),
    path('<int:pk>/regenerate/', ProductRegenerateView.as_view(), name='product-regenerate'),
]
