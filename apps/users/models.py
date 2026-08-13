from typing import TYPE_CHECKING, cast

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models

if TYPE_CHECKING:
    from apps.tenants.models import TenantUser


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        email = self.normalize_email(email)
        user = cast('User', self.model(email=email, **extra_fields))
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """Кастомный пользователь с email вместо username."""

    # Ephemeral request-scoped context selected immediately before JWT issue.
    # This is deliberately not a model field and is absent on ordinary users.
    _current_tenant_membership: 'TenantUser'

    email = models.EmailField(unique=True, verbose_name='Email')
    phone = models.CharField(max_length=20, blank=True, verbose_name='Телефон')
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    is_staff = models.BooleanField(default=False, verbose_name='Сотрудник (доступ в админку)')
    auth_version = models.PositiveIntegerField(
        default=1,
        verbose_name='Версия сессий',
        help_text='Увеличивается при отзыве всех JWT-сессий.',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Дата регистрации')

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        verbose_name = 'Пользователь'
        verbose_name_plural = 'Пользователи'

    def __str__(self):
        return self.email
