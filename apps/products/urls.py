from django.urls import path

from apps.marketplaces.ozon_offer_views import (
    ProductOzonOfferBarcodeView,
    ProductOzonOfferCommerceSyncView,
    ProductOzonOfferBulkView,
    ProductOzonOfferPublishView,
    ProductOzonOfferReconcileView,
    ProductOzonOfferView,
)

from apps.products.views import (
    ProductArchiveView, ProductBulkActionDetailView, ProductBulkActionView,
    ProductBrandOptionsView, ProductCatalogClassificationReviewView, ProductCrossCodesView, ProductDetailView,
    ProductEnrichmentFactReviewView, ProductEnrichmentFactsView, ProductFitmentReviewView, ProductFitmentsView,
    ProductBulkDeleteView, ProductCatalogCategoryAssignView, ProductExcludeView,
    ProductListView, ProductParseJobDetailView,
    ProductParseView, ProductPhysicalProfileView, ProductPhysicalSuggestionReviewView,
    ProductPublishView, ProductRegenerateView,
    ProductReviewQueueActionView,
    ProductReviewQueueView, ProductSearchView,
    ProductSyncView, TenantCatalogCategoryBranchToggleView, TenantCatalogCategoryDefaultImageView,
    TenantCatalogCategoryDetailView, TenantCatalogCategoryListView,
    TenantCategoryMappingDetailView,
    TenantCategoryMappingListView, TenantSourceCategoryListView,
)

urlpatterns = [
    path('ozon-offers/bulk/', ProductOzonOfferBulkView.as_view(), name='product-ozon-offer-bulk'),
    path('', ProductListView.as_view(), name='product-list'),
    path('search/', ProductSearchView.as_view(), name='product-search'),
    path('brand-options/', ProductBrandOptionsView.as_view(), name='product-brand-options'),
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
        'catalog-categories/<int:pk>/toggle-branch/',
        TenantCatalogCategoryBranchToggleView.as_view(),
        name='tenant-catalog-category-toggle-branch',
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
    path(
        '<int:pk>/physical-profile/',
        ProductPhysicalProfileView.as_view(),
        name='product-physical-profile',
    ),
    path(
        '<int:pk>/physical-suggestions/<int:suggestion_id>/<str:action>/',
        ProductPhysicalSuggestionReviewView.as_view(),
        name='product-physical-suggestion-review',
    ),
    path(
        '<int:pk>/ozon-offer/',
        ProductOzonOfferView.as_view(),
        name='product-ozon-offer',
    ),
    path(
        '<int:pk>/ozon-offer/publish/',
        ProductOzonOfferPublishView.as_view(),
        name='product-ozon-offer-publish',
    ),
    path(
        '<int:pk>/ozon-offer/generate-barcode/',
        ProductOzonOfferBarcodeView.as_view(),
        name='product-ozon-offer-generate-barcode',
    ),
    path(
        '<int:pk>/ozon-offer/sync-commerce/',
        ProductOzonOfferCommerceSyncView.as_view(),
        name='product-ozon-offer-sync-commerce',
    ),
    path(
        '<int:pk>/ozon-offer/reconcile/',
        ProductOzonOfferReconcileView.as_view(),
        name='product-ozon-offer-reconcile',
    ),
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
