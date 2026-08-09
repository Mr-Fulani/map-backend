import json
import uuid

from django.core.management.base import BaseCommand, CommandError

from apps.billing.outbox import dispatch_due_billing_outbox


class Command(BaseCommand):
    help = 'Отправить ожидающие события billing transactional outbox'

    def add_arguments(self, parser):
        parser.add_argument(
            '--event-id',
            action='append',
            default=None,
            help='UUID события; параметр можно повторять.',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Максимум событий за запуск (1..1000).',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help=(
                'Игнорировать backoff выбранных pending-событий, но не свежую '
                'processing lease; требует --event-id.'
            ),
        )

    def handle(self, *args, **options):
        limit = options['limit']
        if limit is not None and not 1 <= limit <= 1000:
            raise CommandError('--limit должен быть в диапазоне 1..1000.')

        raw_event_ids = options['event_id']
        if options['force'] and raw_event_ids is None:
            raise CommandError(
                '--force разрешён только вместе с --event-id.',
            )
        event_ids = None
        if raw_event_ids is not None:
            try:
                event_ids = [uuid.UUID(value) for value in raw_event_ids]
            except (AttributeError, TypeError, ValueError) as exc:
                raise CommandError('--event-id должен быть UUID.') from exc

        stats = dispatch_due_billing_outbox(
            event_ids=event_ids,
            limit=limit,
            force=options['force'],
        )
        self.stdout.write(json.dumps(stats, ensure_ascii=False, sort_keys=True))
