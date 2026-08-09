from django.urls import path

from apps.users.views import (
    ChangeEmailRequestView,
    ChangePasswordView,
    ConfirmEmailView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
    UpdateProfileView,
)

urlpatterns = [
    path('auth/profile/', UpdateProfileView.as_view(), name='profile-update'),
    path('auth/change-password/', ChangePasswordView.as_view(), name='change-password'),
    path('auth/change-email/', ChangeEmailRequestView.as_view(), name='change-email'),
    path('auth/confirm-email/', ConfirmEmailView.as_view(), name='confirm-email'),
    path('auth/password-reset/', PasswordResetRequestView.as_view(), name='password-reset'),
    path(
        'auth/password-reset/confirm/',
        PasswordResetConfirmView.as_view(),
        name='password-reset-confirm',
    ),
]
