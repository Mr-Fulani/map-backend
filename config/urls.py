from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.core.admin_views import stats_view

urlpatterns = [
    path('admin/stats/', admin.site.admin_view(stats_view), name='admin-stats'),
    path('admin/', admin.site.urls),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/v1/', include('apps.api.urls')),
    path('api/v1/', include('apps.tenants.urls')),
    path('api/v1/', include('apps.billing.urls')),
    path('api/v1/datasources/', include('apps.datasources.urls')),
    path('api/v1/products/', include('apps.products.urls')),
    path('api/v1/categories/', include('apps.marketplaces.urls')),
    path('api/v1/accounts/', include('apps.marketplaces.account_urls')),
    path('api/v1/listings/', include('apps.marketplaces.listing_urls')),
    path('api/v1/analytics/', include('apps.marketplaces.analytics_urls')),
    path('api/v1/', include('apps.sync.urls')),
]
