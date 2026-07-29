from django.urls import path

from apps.ai_agent.views import AIModelListView, AISettingsView, AIUsageListView

urlpatterns = [
    path('ai/models/', AIModelListView.as_view(), name='ai-model-list'),
    path('ai/settings/', AISettingsView.as_view(), name='ai-settings'),
    path('ai/usage/', AIUsageListView.as_view(), name='ai-usage'),
]
