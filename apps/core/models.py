from django.db import models
from django.utils import timezone


class TimestampedModel(models.Model):
    """Базовая модель с временными метками создания и обновления."""

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Создано')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Обновлено')

    class Meta:
        abstract = True


class SoftDeleteManager(models.Manager):
    """Менеджер, исключающий мягко удалённые записи из выборок по умолчанию."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class SoftDeleteModel(TimestampedModel):
    """Базовая модель с мягким удалением."""

    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    def soft_delete(self):
        self.deleted_at = timezone.now()
        self.save(update_fields=['deleted_at'])

    def restore(self):
        self.deleted_at = None
        self.save(update_fields=['deleted_at'])

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    class Meta:
        abstract = True
