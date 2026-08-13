from django.urls import path

from apps.analytics.views import DashboardSummaryView


urlpatterns = [
    path('dashboard/summary/', DashboardSummaryView.as_view(), name='dashboard-summary'),
]
