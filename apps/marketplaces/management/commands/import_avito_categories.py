"""Management command: импорт дерева категорий Avito в каталог тенантов.

Заливает 192 листа Avito (из data/avito_field_specs.json) как категории каталога
с иерархией и создаёт маппинги категория→Avito с атрибутами. Идемпотентна.
"""
from django.core.management.base import BaseCommand

from apps.marketplaces.avito_category_import import AvitoCatalogImporter
from apps.tenants.models import Tenant


class Command(BaseCommand):
    """Импортирует официальное дерево категорий Avito в каталог тенантов."""

    help = 'Импортирует дерево категорий Avito (avito_field_specs.json) в каталог тенантов + маппинги'

    def add_arguments(self, parser):
        """Параметр: --tenant (slug конкретного тенанта; по умолчанию — все)."""
        parser.add_argument('--tenant', type=str, default=None, help='Slug тенанта (по умолчанию все)')

    def handle(self, *args, **options):
        """Импортирует категории и маппинги для выбранных тенантов."""
        tenants = Tenant.objects.all()
        if options.get('tenant'):
            tenants = tenants.filter(slug=options['tenant'])
            if not tenants.exists():
                self.stderr.write(self.style.ERROR(f'Тенант {options["tenant"]!r} не найден'))
                return

        importer = AvitoCatalogImporter()
        total_categories = 0
        total_mappings = 0
        for tenant in tenants:
            result = importer.import_for_tenant(tenant)
            total_categories += result['categories']
            total_mappings += result['mappings']
            self.stdout.write(
                f'  {tenant.slug}: +{result["categories"]} категорий, +{result["mappings"]} маппингов'
            )

        self.stdout.write(self.style.SUCCESS(
            f'Готово: категорий {total_categories}, маппингов {total_mappings}'
        ))
