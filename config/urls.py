from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/v1/', include('apps.api.urls')),
    path('api/v1/', include('apps.tenants.urls')),
    path('api/v1/', include('apps.billing.urls')),
    path('api/v1/datasources/', include('apps.datasources.urls')),
    path('api/v1/products/', include('apps.products.urls')),
    path('api/v1/categories/', include('apps.marketplaces.urls')),
    path('api/v1/', include('apps.sync.urls')),
]
