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
        every_5m = self._get_interval(5, IntervalSchedule.MINUTES)
        every_15m = self._get_interval(15, IntervalSchedule.MINUTES)
        every_30m = self._get_interval(30, IntervalSchedule.MINUTES)
        every_1h = self._get_interval(60, IntervalSchedule.MINUTES)
        every_6h = self._get_interval(6, IntervalSchedule.HOURS)
        every_7d = self._get_interval(7, IntervalSchedule.DAYS)

        # Cron расписания (время UTC → Moscow UTC+3)
        daily_02 = self._get_crontab(minute=0, hour=23)   # 02:00 MSK = 23:00 UTC
        daily_03 = self._get_crontab(minute=0, hour=0)    # 03:00 MSK = 00:00 UTC
        daily_10 = self._get_crontab(minute=0, hour=7)    # 10:00 MSK = 07:00 UTC

        tasks = [
            {
                'name': 'sync_all_tenants',
                'task': 'apps.sync.tasks.sync_all_tenants',
                'schedule': every_5m,
                'queue': 'sync_import',
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
                'schedule': every_30m,
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
                'name': 'billing_check_expired',
                'task': 'apps.billing.tasks.billing_check_expired',
                'schedule': daily_10,
                'queue': 'billing',
            },
        ]

        for task_def in tasks:
            obj, created = PeriodicTask.objects.update_or_create(
                name=task_def['name'],
                defaults={
                    'task': task_def['task'],
                    'interval' if isinstance(task_def['schedule'], IntervalSchedule) else 'crontab':
                        task_def['schedule'],
                    'queue': task_def['queue'],
                    'enabled': True,
                },
            )
            status = 'создана' if created else 'обновлена'
            self.stdout.write(f'  [{status}] {task_def["name"]}')

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
        )
        return schedule
