from django.urls import path

from apps.notifications.views import (
    NotificationSettingsView,
    NotificationTestView,
    TelegramBotWebhookView,
    TelegramConnectView,
    TelegramDisconnectView,
)

urlpatterns = [
    path('notifications/settings/', NotificationSettingsView.as_view(), name='notification-settings'),
    path('notifications/settings/test/', NotificationTestView.as_view(), name='notification-test'),
    path('notifications/settings/telegram/connect/', TelegramConnectView.as_view(), name='telegram-connect'),
    path('notifications/settings/telegram/', TelegramDisconnectView.as_view(), name='telegram-disconnect'),
    path('notifications/webhook/telegram/', TelegramBotWebhookView.as_view(), name='telegram-bot-webhook'),
]
