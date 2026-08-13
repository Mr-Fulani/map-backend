from django.core.management.base import BaseCommand, CommandError

from apps.web_research.accounting import resolve_web_search_attempt
from apps.web_research.models import WebSearchAttempt


class Command(BaseCommand):
    help = 'Resolve one uncertain paid web-search provider outcome.'

    def add_arguments(self, parser):
        parser.add_argument('--attempt-id', required=True, type=int)
        parser.add_argument('--confirm', required=True)
        parser.add_argument(
            '--action',
            required=True,
            choices=['accepted', 'not_accepted'],
        )
        parser.add_argument('--note', required=True)

    def handle(self, *args, **options):
        if options['confirm'] != str(options['attempt_id']):
            raise CommandError('--confirm must exactly match --attempt-id.')
        try:
            attempt = resolve_web_search_attempt(
                options['attempt_id'],
                action=options['action'],
                operator_note=options['note'],
            )
        except WebSearchAttempt.DoesNotExist as exc:
            raise CommandError('Web search attempt was not found.') from exc
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f'web search attempt {attempt.pk} resolved',
        ))
