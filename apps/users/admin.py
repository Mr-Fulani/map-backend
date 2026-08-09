from django.contrib import admin
from django.db import transaction
from unfold.admin import ModelAdmin
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


class SessionRevokingAdminPasswordChangeForm(AdminPasswordChangeForm):
    """Admin password reset that invalidates every existing JWT session."""

    @transaction.atomic
    def save(self, commit=True):
        if not commit:
            return super().save(commit=False)
        from apps.tenants.session_tokens import revoke_all_user_sessions

        self.user.set_password(self.cleaned_data['password1'])
        self.user.save(update_fields=['password'])
        self.user.auth_version = revoke_all_user_sessions(self.user.pk)
        return self.user


@admin.register(User)
class UserAdmin(ModelAdmin, BaseUserAdmin):
    """Администрирование пользователей системы."""

    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = SessionRevokingAdminPasswordChangeForm

    list_display = ['email', 'is_active', 'is_staff', 'created_at']
    list_filter = ['is_active', 'is_staff']
    search_fields = ['email']
    ordering = ['-created_at']
    readonly_fields = ['auth_version', 'created_at']
    fieldsets = (
        ('Учётная запись', {'fields': ('email', 'password')}),
        ('Персональные данные', {'fields': ('phone',)}),
        ('Права доступа', {
            'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions'),
            'classes': ('collapse',),
        }),
        ('Служебное', {'fields': ('auth_version', 'created_at'), 'classes': ('collapse',)}),
    )
    add_fieldsets = (
        ('Новый пользователь', {'classes': ('wide',), 'fields': ('email', 'password1', 'password2')}),
    )

    def save_model(self, request, obj, form, change):
        """Revoke sessions when admin changes account identity or security state."""
        security_fields = ('email', 'password', 'is_active', 'is_staff', 'is_superuser')
        original = None
        if change and obj.pk:
            original = User.objects.filter(pk=obj.pk).values(*security_fields).first()

        super().save_model(request, obj, form, change)

        if original and any(original[field] != getattr(obj, field) for field in security_fields):
            from apps.tenants.session_tokens import revoke_all_user_sessions
            obj.auth_version = revoke_all_user_sessions(obj.pk)
