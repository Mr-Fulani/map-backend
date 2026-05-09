from django.urls import path

from apps.marketplaces.views import AnalyticsView

urlpatterns = [
    path('', AnalyticsView.as_view(), name='analytics'),
]
