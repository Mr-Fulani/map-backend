from django.urls import path

from apps.users.views import (
    ChangeEmailRequestView,
    ChangePasswordView,
    ConfirmEmailView,
    UpdateProfileView,
)

urlpatterns = [
    path('auth/profile/', UpdateProfileView.as_view(), name='profile-update'),
    path('auth/change-password/', ChangePasswordView.as_view(), name='change-password'),
    path('auth/change-email/', ChangeEmailRequestView.as_view(), name='change-email'),
    path('auth/confirm-email/', ConfirmEmailView.as_view(), name='confirm-email'),
]
