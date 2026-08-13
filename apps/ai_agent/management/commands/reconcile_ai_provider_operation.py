import json
import uuid

from django.core.management.base import BaseCommand, CommandError

from apps.ai_agent.models import AIProviderOperation
from apps.ai_agent.reconciliation import (
    AIProviderOperationStateError, resolve_uncertain_ai_provider_operation,
)


class Command(BaseCommand):
    help = (
        'Вручную и идемпотентно разрешает одну неопределённую операцию '
        'AI-провайдера. Команда никогда не выполняет массовую сверку.'
    )

    def add_arguments(self, parser):
        parser.add_argument('operation_id', type=uuid.UUID)
        parser.add_argument(
            '--action',
            required=True,
            choices=['release', 'settle-reserved'],
            help=(
                'release возвращает резерв; settle-reserved списывает ровно '
                'зарезервированную сумму.'
            ),
        )
        parser.add_argument(
            '--note',
            required=True,
            help='Обязательное обоснование решения оператора.',
        )
        parser.add_argument(
            '--confirm',
            required=True,
            type=uuid.UUID,
            help='Для подтверждения повторите UUID операции.',
        )

    def handle(self, *args, **options):
        operation_id = options['operation_id']
        if options['confirm'] != operation_id:
            raise CommandError('--confirm должен точно совпадать с operation_id.')

        note = options['note'].strip()
        if not note:
            raise CommandError('--note не может быть пустым.')
        if len(note) > 4000:
            raise CommandError('--note не может быть длиннее 4000 символов.')

        action = options['action'].replace('-', '_')
        try:
            operation, changed = resolve_uncertain_ai_provider_operation(
                operation_id,
                action=action,
                operator_note=note,
            )
        except AIProviderOperation.DoesNotExist as exc:
            raise CommandError('Операция AI-провайдера не найдена.') from exc
        except (AIProviderOperationStateError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        # Deliberately omit the reservation key and operator note from stdout.
        self.stdout.write(json.dumps({
            'operation_id': str(operation.pk),
            'status': operation.status,
            'changed': changed,
            'charged_amount': (
                str(operation.charged_amount)
                if operation.charged_amount is not None else None
            ),
        }, ensure_ascii=False, sort_keys=True))
