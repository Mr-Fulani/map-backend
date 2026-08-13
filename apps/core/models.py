import uuid

from django.db import models
from django.utils import timezone


class TimestampedModel(models.Model):
    """Базовая модель с временными метками создания и обновления."""

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Создано')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Обновлено')

    class Meta:
        abstract = True


class BackgroundJobDispatch(TimestampedModel):
    """Durable PostgreSQL-backed delivery record for user initiated jobs.

    Celery is deliberately only the transport.  The database row is the source
    of truth, so a broker outage, a lost delivery or a killed worker can be
    recovered by the periodic dispatcher without recreating the domain job.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Ожидает отправки'
        PUBLISHING = 'publishing', 'Отправляется'
        PUBLISHED = 'published', 'Отправлено'
        RUNNING = 'running', 'Выполняется'
        SUCCEEDED = 'succeeded', 'Завершено'
        FAILED = 'failed', 'Ошибка'
        CANCELLED = 'cancelled', 'Отменено'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task_name = models.CharField(max_length=255, verbose_name='Celery task')
    queue = models.CharField(max_length=64, verbose_name='Очередь')
    args = models.JSONField(default=list, blank=True, verbose_name='Позиционные аргументы')
    kwargs = models.JSONField(default=dict, blank=True, verbose_name='Именованные аргументы')
    deduplication_key = models.CharField(
        max_length=255, null=True, blank=True, unique=True,
        verbose_name='Ключ дедупликации',
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING,
        db_index=True, verbose_name='Статус',
    )
    available_at = models.DateTimeField(
        default=timezone.now, db_index=True, verbose_name='Доступно после',
    )
    claim_token = models.UUIDField(null=True, blank=True, editable=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    celery_task_id = models.UUIDField(null=True, blank=True, editable=False)
    publish_attempts = models.PositiveIntegerField(default=0)
    run_attempts = models.PositiveIntegerField(default=0)
    max_run_attempts = models.PositiveSmallIntegerField(default=5)
    execution_timeout_seconds = models.PositiveIntegerField(default=3700)
    last_error = models.TextField(blank=True)
    result = models.JSONField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Надёжная фоновая задача'
        verbose_name_plural = 'Надёжные фоновые задачи'
        indexes = [
            models.Index(
                fields=['status', 'available_at'],
                name='core_job_status_due_idx',
            ),
            models.Index(
                fields=['status', 'lease_expires_at'],
                name='core_job_lease_idx',
            ),
            models.Index(
                fields=['status', 'finished_at'],
                name='core_job_finished_idx',
            ),
            models.Index(
                fields=['task_name', '-created_at'],
                name='core_job_task_created_idx',
            ),
        ]

    def __str__(self):
        return f'{self.task_name} [{self.status}] {self.pk}'


class PaidIngressIntent(TimestampedModel):
    """Canonical durable identity for a user-triggered paid operation.

    The client UUID is only unique inside one tenant and operation.  The
    fingerprint binds it to the complete canonical request, while the result
    fields let an ambiguous HTTP retry recover the already-created run/job
    without consuming another budget unit or dispatching another provider
    call.
    """

    tenant = models.ForeignKey(
        'tenants.Tenant',
        on_delete=models.CASCADE,
        related_name='paid_ingress_intents',
    )
    operation = models.SlugField(max_length=80)
    idempotency_key = models.UUIDField(default=uuid.uuid4, editable=False)
    request_fingerprint = models.CharField(max_length=64, editable=False)
    raw_payload_fingerprint = models.CharField(max_length=64, editable=False)
    request_payload = models.JSONField(default=dict, editable=False)
    resource_type = models.CharField(max_length=80, editable=False)
    resource_id = models.CharField(max_length=80, editable=False)
    result_type = models.CharField(max_length=80, blank=True, editable=False)
    result_id = models.CharField(max_length=80, blank=True, editable=False)
    result_metadata = models.JSONField(default=dict, blank=True, editable=False)
    dispatch = models.ForeignKey(
        BackgroundJobDispatch,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='paid_ingress_intents',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'operation', 'idempotency_key'],
                name='uniq_tenant_paid_ingress',
            ),
        ]
        indexes = [
            models.Index(
                fields=['tenant', '-created_at'],
                name='paid_intent_tenant_created_idx',
            ),
        ]

    def __str__(self):
        return f'{self.operation}:{self.idempotency_key}'


class TenantDailyPaidUsage(TimestampedModel):
    """Rollback-safe daily accounting for transaction-created paid work."""

    tenant = models.ForeignKey(
        'tenants.Tenant',
        on_delete=models.CASCADE,
        related_name='daily_paid_usage',
    )
    scope = models.SlugField(max_length=80)
    usage_date = models.DateField()
    units = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'scope', 'usage_date'],
                name='uniq_tenant_daily_paid_usage',
            ),
            models.CheckConstraint(
                condition=models.Q(units__gte=0),
                name='daily_paid_usage_nonnegative',
            ),
        ]
        indexes = [
            models.Index(
                fields=['scope', 'usage_date'],
                name='paid_usage_scope_date_idx',
            ),
        ]

    def __str__(self):
        return f'{self.tenant_id}:{self.scope}:{self.usage_date}={self.units}'


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


SoftDeleteManagerBase = models.Manager.from_queryset(SoftDeleteQuerySet)


class SoftDeleteManager(SoftDeleteManagerBase):  # type: ignore[misc, valid-type]
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
