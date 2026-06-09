from django.urls import path

from apps.products.views import (
    ProductArchiveView, ProductBulkActionDetailView, ProductBulkActionView,
    ProductCatalogClassificationReviewView, ProductCrossCodesView, ProductDetailView,
    ProductEnrichmentFactReviewView, ProductEnrichmentFactsView, ProductFitmentReviewView, ProductFitmentsView,
    ProductBulkDeleteView, ProductCatalogCategoryAssignView, ProductExcludeView, ProductListView, ProductParseJobDetailView,
    ProductParseView, ProductPublishView, ProductRegenerateView, ProductReviewQueueActionView,
    ProductReviewQueueView, ProductSearchView,
    ProductSyncView, TenantCatalogCategoryDefaultImageView,
    TenantCatalogCategoryDetailView, TenantCatalogCategoryListView,
    TenantCategoryMappingDetailView,
    TenantCategoryMappingListView, TenantSourceCategoryListView,
)

urlpatterns = [
    path('', ProductListView.as_view(), name='product-list'),
    path('search/', ProductSearchView.as_view(), name='product-search'),
    path('parse/', ProductParseView.as_view(), name='product-parse'),
    path('parse-jobs/<int:pk>/', ProductParseJobDetailView.as_view(), name='product-parse-job-detail'),
    path('bulk-actions/', ProductBulkActionView.as_view(), name='product-bulk-action'),
    path('bulk-actions/<int:pk>/', ProductBulkActionDetailView.as_view(), name='product-bulk-action-detail'),
    path('review-queue/', ProductReviewQueueView.as_view(), name='product-review-queue'),
    path(
        'review-queue/<str:item_type>/<int:record_id>/<str:action>/',
        ProductReviewQueueActionView.as_view(),
        name='product-review-queue-action',
    ),
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
        'catalog-categories/<int:pk>/default-image/',
        TenantCatalogCategoryDefaultImageView.as_view(),
        name='tenant-catalog-category-default-image',
    ),
    path(
        'catalog-category-mappings/',
        TenantCategoryMappingListView.as_view(),
        name='tenant-category-mapping-list',
    ),
    path(
        'catalog-category-mappings/<int:pk>/',
        TenantCategoryMappingDetailView.as_view(),
        name='tenant-category-mapping-detail',
    ),
    path(
        'catalog-source-categories/',
        TenantSourceCategoryListView.as_view(),
        name='tenant-source-category-list',
    ),
    path('exclude/', ProductExcludeView.as_view(), name='product-exclude'),
    path('bulk-delete/', ProductBulkDeleteView.as_view(), name='product-bulk-delete'),
    path('sync/<int:connection_id>/', ProductSyncView.as_view(), name='product-sync'),
    path('<int:pk>/', ProductDetailView.as_view(), name='product-detail'),
    path('<int:pk>/fitments/', ProductFitmentsView.as_view(), name='product-fitments'),
    path(
        '<int:pk>/fitments/<int:fitment_id>/<str:action>/',
        ProductFitmentReviewView.as_view(),
        name='product-fitment-review',
    ),
    path('<int:pk>/enrichment-facts/', ProductEnrichmentFactsView.as_view(), name='product-enrichment-facts'),
    path(
        '<int:pk>/enrichment-facts/<int:fact_id>/<str:action>/',
        ProductEnrichmentFactReviewView.as_view(),
        name='product-enrichment-fact-review',
    ),
    path(
        '<int:pk>/catalog-classification/<str:action>/',
        ProductCatalogClassificationReviewView.as_view(),
        name='product-catalog-classification-review',
    ),
    path('<int:pk>/cross-codes/', ProductCrossCodesView.as_view(), name='product-cross-codes'),
    path('<int:pk>/publish/', ProductPublishView.as_view(), name='product-publish'),
    path('<int:pk>/archive/', ProductArchiveView.as_view(), name='product-archive'),
    path('<int:pk>/regenerate/', ProductRegenerateView.as_view(), name='product-regenerate'),
]
