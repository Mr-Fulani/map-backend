from django.core.management.base import BaseCommand

from apps.core.retention import purge_retained_data


class Command(BaseCommand):
    help = 'Физически удаляет данные после истечения настроенных retention-сроков.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        result = purge_retained_data(dry_run=options['dry_run'])
        prefix = '[dry-run] ' if options['dry_run'] else ''
        for name, count in result.items():
            self.stdout.write(f'{prefix}{name}: {count}')
