from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from unfold.admin import ModelAdmin

from .models import User


@admin.register(User)
class UserAdmin(ModelAdmin, BaseUserAdmin):
    """Администрирование пользователей системы."""

    list_display = ['email', 'is_active', 'is_staff', 'created_at']
    list_filter = ['is_active', 'is_staff']
    search_fields = ['email']
    ordering = ['-created_at']
    readonly_fields = ['created_at']
    fieldsets = (
        ('Учётная запись', {'fields': ('email', 'password')}),
        ('Персональные данные', {'fields': ('phone',)}),
        ('Права доступа', {
            'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions'),
            'classes': ('collapse',),
        }),
        ('Служебное', {'fields': ('created_at',), 'classes': ('collapse',)}),
    )
    add_fieldsets = (
        ('Новый пользователь', {'classes': ('wide',), 'fields': ('email', 'password1', 'password2')}),
    )
