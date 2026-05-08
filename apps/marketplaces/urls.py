from django.urls import path

from apps.marketplaces.views import CategoryMappingDetailView, CategoryMappingListView, UnmappedCategoriesView

urlpatterns = [
    path('unmapped/', UnmappedCategoriesView.as_view(), name='categories-unmapped'),
    path('mappings/', CategoryMappingListView.as_view(), name='category-mapping-list'),
    path('mappings/<int:pk>/', CategoryMappingDetailView.as_view(), name='category-mapping-detail'),
]
