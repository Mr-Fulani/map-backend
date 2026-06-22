from django.urls import path

from apps.browser_sessions.views import (
    BrowserLoginStatusView,
    BrowserSessionView,
    BrowserVerifyView,
    PublishMethodView,
)

# Подключается внутри /api/v1/accounts/<pk>/
urlpatterns = [
    path('publish-method/', PublishMethodView.as_view(), name='browser-publish-method'),
    path('browser-login/', BrowserLoginStatusView.as_view(), name='browser-login-status'),
    path('browser-verify/', BrowserVerifyView.as_view(), name='browser-verify'),
    path('browser-session/', BrowserSessionView.as_view(), name='browser-session'),
]
