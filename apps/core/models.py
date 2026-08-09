from django.db import models
from django.utils import timezone


class TimestampedModel(models.Model):
    """Базовая модель с временными метками создания и обновления."""

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Создано')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Обновлено')

    class Meta:
        abstract = True


class SoftDeleteQuerySet(models.QuerySet):
    """QuerySet, в котором delete безопасно превращён в soft-delete."""

    def delete(self):
        deleted_at = timezone.now()
        count = self.update(deleted_at=deleted_at)
        return count, {self.model._meta.label: count}

    def hard_delete(self):
        return super().delete()

    def restore(self):
        return self.update(deleted_at=None)


class SoftDeleteManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """Менеджер, исключающий мягко удалённые записи из выборок по умолчанию."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class SoftDeleteModel(TimestampedModel):
    """Базовая модель с мягким удалением."""

    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    def delete(self, using=None, keep_parents=False):
        """Скрывает запись, сохраняя связанные данные до retention purge."""
        self.soft_delete()
        return 1, {self._meta.label: 1}

    def hard_delete(self, using=None, keep_parents=False):
        """Физическое удаление разрешено только retention/admin workflow."""
        return super().delete(using=using, keep_parents=keep_parents)

    def soft_delete(self):
        if self.deleted_at is not None:
            return
        self.deleted_at = timezone.now()
        self.save(update_fields=['deleted_at', 'updated_at'])

    def restore(self):
        self.deleted_at = None
        self.save(update_fields=['deleted_at', 'updated_at'])

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    class Meta:
        abstract = True
