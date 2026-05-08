from django.urls import path

from apps.sync.views import SyncLogListView

urlpatterns = [
    path('logs/', SyncLogListView.as_view(), name='sync-log-list'),
]
