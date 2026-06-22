from django.contrib import admin
from unfold.admin import ModelAdmin

from apps.browser_sessions.models import BrowserSession


@admin.register(BrowserSession)
class BrowserSessionAdmin(ModelAdmin):
    """Администрирование браузерных сессий (web-режим Avito)."""

    list_display = [
        'account', 'browser_type', 'session_valid', 'sms_pending', 'last_login_at', 'updated_at',
    ]
    list_filter = ['browser_type', 'session_valid', 'sms_pending']
    readonly_fields = [
        'account', 'browser_type', 'profile_dir', 'fingerprint',
        'session_valid', 'sms_pending', 'last_login_at', 'created_at', 'updated_at',
    ]
    search_fields = ['account__name', 'account__tenant__slug']
    ordering = ['-updated_at']

    def has_add_permission(self, request):
        return False
