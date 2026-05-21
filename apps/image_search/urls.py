"""URL-маршруты для API управления изображениями товаров."""

from django.urls import path

from apps.image_search.views import (
    BulkSearchView,
    ImageApproveView,
    ImageDetailView,
    ImageListView,
    ImageRejectView,
    ImageSearchStatusView,
    ImageSearchView,
    ImageSetPrimaryView,
    ImageUploadView,
)

urlpatterns = [
    # Список и массовые операции
    path(
        'products/<int:product_pk>/images/',
        ImageListView.as_view(),
        name='product-images-list',
    ),
    path(
        'products/<int:product_pk>/images/upload/',
        ImageUploadView.as_view(),
        name='product-images-upload',
    ),
    path(
        'products/<int:product_pk>/images/search/',
        ImageSearchView.as_view(),
        name='product-images-search',
    ),
    path(
        'products/<int:product_pk>/images/search/<str:task_id>/',
        ImageSearchStatusView.as_view(),
        name='product-images-search-status',
    ),
    # Операции с конкретным изображением
    path(
        'products/<int:product_pk>/images/<int:image_pk>/',
        ImageDetailView.as_view(),
        name='product-images-detail',
    ),
    path(
        'products/<int:product_pk>/images/<int:image_pk>/approve/',
        ImageApproveView.as_view(),
        name='product-images-approve',
    ),
    path(
        'products/<int:product_pk>/images/<int:image_pk>/reject/',
        ImageRejectView.as_view(),
        name='product-images-reject',
    ),
    path(
        'products/<int:product_pk>/images/<int:image_pk>/set_primary/',
        ImageSetPrimaryView.as_view(),
        name='product-images-set-primary',
    ),
    # Массовый поиск
    path(
        'images/bulk-search/',
        BulkSearchView.as_view(),
        name='images-bulk-search',
    ),
]
