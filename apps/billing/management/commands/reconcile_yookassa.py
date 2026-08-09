import json

from django.core.management.base import BaseCommand, CommandError

from apps.billing.reconciliation import reconcile_yookassa_billing


class Command(BaseCommand):
    help = (
        'Сверяет ошибки webhook и незавершённые платежи с YooKassa '
        '(безопасно и идемпотентно)'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit',
            type=int,
            help='Максимальное число событий и счетов за один запуск (1..1000).',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Игнорировать только backoff; hard limit попыток сохраняется.',
        )
        parser.add_argument(
            '--event-id',
            type=int,
            action='append',
            dest='event_ids',
            help='Сверить конкретный BillingWebhookEvent; можно указать несколько раз.',
        )
        parser.add_argument(
            '--invoice-id',
            type=int,
            action='append',
            dest='invoice_ids',
            help='Сверить конкретный Invoice; можно указать несколько раз.',
        )

    def handle(self, *args, **options):
        limit = options['limit']
        if limit is not None and not 1 <= limit <= 1000:
            raise CommandError('--limit должен быть в диапазоне 1..1000.')
        if options['force'] and not (options['event_ids'] or options['invoice_ids']):
            raise CommandError(
                '--force разрешён только вместе с --event-id или --invoice-id.',
            )

        event_ids = options['event_ids']
        invoice_ids = options['invoice_ids']
        # None means the normal periodic sweep; [] explicitly skips that class.
        # A targeted CLI call must never sweep the untargeted class by accident.
        if event_ids is not None and invoice_ids is None:
            invoice_ids = []
        elif invoice_ids is not None and event_ids is None:
            event_ids = []

        result = reconcile_yookassa_billing(
            limit=limit,
            force=options['force'],
            event_ids=event_ids,
            invoice_ids=invoice_ids,
        )
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
