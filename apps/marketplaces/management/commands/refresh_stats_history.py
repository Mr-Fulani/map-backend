"""Management command для загрузки исторической статистики листингов из Avito Stats API."""
import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.marketplaces.models import MarketplaceAccount
from apps.marketplaces.services import StatsService


class Command(BaseCommand):
    """Загружает историческую статистику листингов из Avito Stats API."""

    help = 'Загружает статистику листингов за последние N дней из Avito Stats API'

    def add_arguments(self, parser):
        """Добавляет параметр --days (по умолчанию 30)."""
        parser.add_argument(
            '--days', type=int, default=30,
            help='Количество дней истории (по умолчанию 30)',
        )
        parser.add_argument(
            '--account', type=int, default=None,
            help='ID конкретного аккаунта (по умолчанию все активные)',
        )

    def handle(self, *args, **options):
        """Запускает загрузку статистики для всех активных аккаунтов."""
        days = int(options['days'])
        if not 1 <= days <= StatsService.MAX_HISTORY_DAYS:
            raise CommandError('--days must be between 1 and 270.')
        today = timezone.localdate()
        date_from = today - datetime.timedelta(days=days - 1)

        qs = MarketplaceAccount.objects.filter(
            is_active=True,
            tenant__is_active=True,
            marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        ).select_related('tenant')
        if options['account']:
            qs = qs.filter(pk=options['account'])

        accounts = list(qs)
        if not accounts:
            self.stdout.write(self.style.WARNING('Активных аккаунтов не найдено.'))
            return

        self.stdout.write(
            f'Загрузка статистики за {date_from} — {today} '
            f'для {len(accounts)} аккаунт(ов)...'
        )

        total = 0
        for account in accounts:
            try:
                count = StatsService.fetch_for_account(account, date_from, today)
                total += count
                self.stdout.write(
                    self.style.SUCCESS(
                        f'  {account.tenant.slug} / {account.name}: {count} записей'
                    )
                )
            except Exception as exc:
                self.stdout.write(
                    self.style.ERROR(f'  {account.tenant.slug} / {account.name}: ошибка — {exc}')
                )

        self.stdout.write(self.style.SUCCESS(f'\nИтого: {total} записей ListingStats'))
