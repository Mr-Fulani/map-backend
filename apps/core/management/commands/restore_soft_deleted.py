from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from apps.core.models import SoftDeleteModel


class Command(BaseCommand):
    help = 'Восстанавливает soft-deleted запись по app.Model и primary key.'

    def add_arguments(self, parser):
        parser.add_argument('model', help='Например: products.Product')
        parser.add_argument('pk')
        parser.add_argument('--reactivate', action='store_true')

    def handle(self, *args, **options):
        try:
            model = apps.get_model(options['model'])
        except (LookupError, ValueError) as exc:
            raise CommandError(f'Неизвестная модель: {options["model"]}') from exc
        if not issubclass(model, SoftDeleteModel):
            raise CommandError('Модель не поддерживает soft-delete.')
        try:
            instance = model.all_objects.get(pk=options['pk'], deleted_at__isnull=False)
        except model.DoesNotExist as exc:
            raise CommandError('Удалённая запись не найдена.') from exc
        instance.restore()
        if options['reactivate'] and hasattr(instance, 'is_active'):
            instance.is_active = True
            instance.save(update_fields=['is_active', 'updated_at'])
        self.stdout.write(self.style.SUCCESS(f'Восстановлено: {model._meta.label} pk={instance.pk}'))
