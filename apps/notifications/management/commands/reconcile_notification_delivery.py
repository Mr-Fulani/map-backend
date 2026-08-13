from datetime import timedelta
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.notifications.models import NotificationDelivery


class Command(BaseCommand):
    help = 'Явно закрывает неоднозначную доставку уведомления после внешней сверки.'

    def add_arguments(self, parser):
        parser.add_argument('delivery_id')
        parser.add_argument('--action', required=True, choices=['sent', 'failed'])
        parser.add_argument('--note', required=True)
        parser.add_argument('--confirm', required=True)

    def handle(self, *args, **options):
        try:
            delivery_id = uuid.UUID(options['delivery_id'])
            confirmation = uuid.UUID(options['confirm'])
        except (TypeError, ValueError) as exc:
            raise CommandError('delivery_id/confirm должны быть UUID.') from exc
        if confirmation != delivery_id:
            raise CommandError('--confirm должен точно совпадать с delivery_id.')
        note = str(options['note']).strip()
        if not note:
            raise CommandError('--note обязателен.')

        with transaction.atomic():
            try:
                delivery = NotificationDelivery.objects.select_for_update().get(
                    pk=delivery_id,
                )
            except NotificationDelivery.DoesNotExist as exc:
                raise CommandError('Доставка не найдена.') from exc
            if delivery.reconciled_at is not None:
                if delivery.reconciliation_action != options['action']:
                    raise CommandError('Доставка уже сверена с другим результатом.')
                self.stdout.write(self.style.SUCCESS('Delivery already reconciled.'))
                return
            if delivery.status == NotificationDelivery.Status.SENDING:
                stale_before = timezone.now() - timedelta(
                    seconds=settings.NOTIFICATION_DELIVERY_CLAIM_TIMEOUT_SECONDS,
                )
                if delivery.claimed_at is None or delivery.claimed_at > stale_before:
                    raise CommandError('Доставка ещё может выполняться.')
            elif delivery.status != NotificationDelivery.Status.OUTCOME_UNCERTAIN:
                raise CommandError('Только неоднозначная доставка может быть сверена.')

            delivery.status = (
                NotificationDelivery.Status.SENT
                if options['action'] == 'sent'
                else NotificationDelivery.Status.FAILED
            )
            delivery.reconciliation_action = options['action']
            delivery.reconciliation_note = note
            delivery.reconciled_at = timezone.now()
            delivery.finished_at = delivery.reconciled_at
            delivery.save(update_fields=[
                'status', 'reconciliation_action', 'reconciliation_note',
                'reconciled_at', 'finished_at', 'updated_at',
            ])

        self.stdout.write(self.style.SUCCESS('Notification delivery reconciled.'))
