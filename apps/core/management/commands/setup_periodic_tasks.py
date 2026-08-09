from django.core.management.base import BaseCommand
from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask


class Command(BaseCommand):
    """
    Создаёт или обновляет все PeriodicTask для Celery Beat.

    Идемпотентен: повторный запуск не создаёт дубликаты.
    Использовать после деплоя или при изменении расписания задач.
    """

    help = 'Настроить PeriodicTask для Celery Beat (идемпотентно)'

    def handle(self, *args, **options):
        """Создаёт все задачи по расписанию."""
        self._setup_tasks()
        self.stdout.write(self.style.SUCCESS('PeriodicTask настроены успешно.'))

    def _setup_tasks(self):
        """Регистрирует все периодические задачи."""
        # Интервальные расписания
        every_1m = self._get_interval(1, IntervalSchedule.MINUTES)
        every_5m = self._get_interval(5, IntervalSchedule.MINUTES)
        every_10m = self._get_interval(10, IntervalSchedule.MINUTES)
        every_15m = self._get_interval(15, IntervalSchedule.MINUTES)
        every_1h = self._get_interval(60, IntervalSchedule.MINUTES)
        every_6h = self._get_interval(6, IntervalSchedule.HOURS)
        every_7d = self._get_interval(7, IntervalSchedule.DAYS)
        every_72h = self._get_interval(72, IntervalSchedule.HOURS)

        # Crontab хранит явную timezone, поэтому часы указываются сразу по Москве.
        daily_02 = self._get_crontab(minute=0, hour=2)
        daily_03 = self._get_crontab(minute=0, hour=3)
        daily_04 = self._get_crontab(minute=0, hour=4)
        daily_10 = self._get_crontab(minute=0, hour=10)

        tasks = [
            {
                'name': 'dispatch_due_product_bulk_jobs',
                'task': 'apps.products.tasks.dispatch_due_product_bulk_jobs',
                'schedule': every_1m,
                'queue': 'part_parsing_bulk',
            },
            {
                'name': 'dispatch_pending_webhooks',
                'task': 'apps.tenants.tasks.dispatch_pending_webhooks',
                'schedule': every_1m,
                'queue': 'notifications',
            },
            {
                'name': 'dispatch_billing_outbox',
                'task': 'apps.billing.tasks.dispatch_billing_outbox',
                'schedule': every_1m,
                'queue': 'billing',
            },
            {
                'name': 'sync_all_tenants',
                'task': 'apps.sync.tasks.sync_all_tenants',
                'schedule': every_5m,
                'queue': 'sync_import',
            },
            {
                'name': 'reconcile_yookassa_billing',
                'task': 'apps.billing.tasks.reconcile_yookassa_billing',
                'schedule': every_5m,
                'queue': 'billing',
            },
            {
                'name': 'update_tenant_counters',
                'task': 'apps.tenants.tasks.update_tenant_counters',
                'schedule': every_15m,
                'queue': 'sync_import',
            },
            {
                'name': 'check_moderation_status',
                'task': 'apps.marketplaces.tasks.check_moderation_status',
                'schedule': every_10m,
                'queue': 'avito_update',
            },
            {
                'name': 'refresh_avito_stats',
                'task': 'apps.marketplaces.tasks.refresh_avito_stats',
                'schedule': every_1h,
                'queue': 'avito_update',
            },
            {
                'name': 'refresh_avito_account_statuses',
                'task': 'apps.marketplaces.tasks.refresh_avito_account_statuses',
                'schedule': every_6h,
                'queue': 'avito_update',
            },
            {
                'name': 'sync_avito_category_tree',
                'task': 'apps.marketplaces.tasks.sync_avito_category_tree',
                'schedule': every_7d,
                'queue': 'avito_update',
            },
            {
                'name': 'sync_avito_brand_catalog',
                'task': 'apps.marketplaces.tasks.sync_avito_brand_catalog',
                'schedule': every_72h,
                'queue': 'avito_update',
            },
            {
                'name': 'cleanup_old_logs',
                'task': 'apps.notifications.tasks.cleanup_old_logs',
                'schedule': daily_02,
                'queue': 'notifications',
            },
            {
                'name': 'reconcile_listings',
                'task': 'apps.marketplaces.tasks.reconcile_listings',
                'schedule': daily_03,
                'queue': 'avito_update',
            },
            {
                'name': 'purge_retained_data',
                'task': 'apps.core.tasks.purge_retained_data_task',
                'schedule': daily_04,
                'queue': 'notifications',
            },
            {
                'name': 'billing_check_expired',
                'task': 'apps.billing.tasks.billing_check_expired',
                'schedule': daily_10,
                'queue': 'billing',
            },
            {
                'name': 'reset_monthly_ai_credits',
                'task': 'apps.billing.tasks.reset_monthly_ai_credits',
                'schedule': daily_10,
                'queue': 'billing',
            },
        ]

        for task_def in tasks:
            schedule_field = (
                'interval'
                if isinstance(task_def['schedule'], IntervalSchedule)
                else 'crontab'
            )
            defaults = {
                'task': task_def['task'],
                'interval': None,
                'crontab': None,
                'queue': task_def['queue'],
                'enabled': True,
            }
            defaults[schedule_field] = task_def['schedule']
            obj, created = PeriodicTask.objects.update_or_create(
                name=task_def['name'],
                defaults=defaults,
            )
            status = 'создана' if created else 'обновлена'
            self.stdout.write(f'  [{status}] {task_def["name"]}')

        # До перехода на единый DatabaseScheduler эти записи создавались из
        # CELERY_BEAT_SCHEDULE под другими именами и могли запускать задачи дважды.
        legacy_names = [
            'sync-avito-brand-catalog-72h',
            'cleanup-old-logs-daily',
            'billing-check-expired-daily',
            'reset-monthly-ai-credits-daily',
            'update-tenant-counters-15min',
            'check-moderation-status-10min',
            'refresh-avito-account-statuses-6h',
            'sync-avito-category-tree-weekly',
        ]
        disabled = PeriodicTask.objects.filter(
            name__in=legacy_names,
            enabled=True,
        ).update(enabled=False)
        if disabled:
            self.stdout.write(f'  [отключено legacy-задач: {disabled}]')

    def _get_interval(self, every: int, period: str) -> IntervalSchedule:
        """Возвращает или создаёт IntervalSchedule."""
        schedule, _ = IntervalSchedule.objects.get_or_create(every=every, period=period)
        return schedule

    def _get_crontab(self, minute: int = 0, hour: int = 0) -> CrontabSchedule:
        """Возвращает или создаёт CrontabSchedule."""
        schedule, _ = CrontabSchedule.objects.get_or_create(
            minute=str(minute),
            hour=str(hour),
            day_of_week='*',
            day_of_month='*',
            month_of_year='*',
            timezone='Europe/Moscow',
        )
        return schedule
