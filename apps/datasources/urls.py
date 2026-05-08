from django.urls import path

from apps.datasources.views import CSVUploadView, DataSourceDetailView, DataSourceListView, DataSourceTestView

urlpatterns = [
    path('', DataSourceListView.as_view(), name='datasource-list'),
    path('<int:pk>/', DataSourceDetailView.as_view(), name='datasource-detail'),
    path('<int:pk>/test/', DataSourceTestView.as_view(), name='datasource-test'),
    path('upload-csv/', CSVUploadView.as_view(), name='datasource-upload-csv'),
]
