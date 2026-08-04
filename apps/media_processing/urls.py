from django.urls import path

from apps.media_processing.views import (
    ImageAssessmentListView,
    MediaJobDetailView,
    MediaJobListView,
    MediaPresetListCreateView,
    MediaProviderListView,
    ProductImageProcessView,
    ProductImageVariantActivateView,
    TenantMediaSettingsView,
)


urlpatterns = [
    path('media/providers/', MediaProviderListView.as_view(), name='media-provider-list'),
    path('media/presets/', MediaPresetListCreateView.as_view(), name='media-preset-list'),
    path('media/settings/', TenantMediaSettingsView.as_view(), name='media-settings'),
    path('media/jobs/', MediaJobListView.as_view(), name='media-job-list'),
    path('media/jobs/<int:job_pk>/', MediaJobDetailView.as_view(), name='media-job-detail'),
    path('media/assessments/', ImageAssessmentListView.as_view(), name='media-assessment-list'),
    path(
        'products/<int:product_pk>/images/<int:image_pk>/process/',
        ProductImageProcessView.as_view(),
        name='product-image-process',
    ),
    path(
        'media/variants/<int:variant_pk>/activate/',
        ProductImageVariantActivateView.as_view(),
        name='media-variant-activate',
    ),
]
