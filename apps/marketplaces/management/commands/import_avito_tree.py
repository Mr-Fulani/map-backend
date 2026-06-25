"""Management command: импорт вшитого дерева категорий Avito в каталог тенантов.

Строит у тенантов полное дерево категорий домена (из avito_tree_<domain>.json,
который готовит sync_avito_full_tree). Идемпотентно.

Примеры:
    python manage.py import_avito_tree --domain auto_parts
    python manage.py import_avito_tree --domain auto_parts --tenant alfapro
"""
from django.core.management.base import BaseCommand

from apps.marketplaces.avito_tree_import import AvitoTreeImporter, has_tree
from apps.tenants.models import Tenant


class Command(BaseCommand):
    """Импортирует дерево категорий Avito домена в каталог тенантов."""

    help = 'Импортирует вшитое дерево категорий Avito (avito_tree_<domain>.json) в каталог тенантов'

    def add_arguments(self, parser):
        parser.add_argument('--domain', type=str, required=True, help='Slug домена (есть JSON-дерево)')
        parser.add_argument('--tenant', type=str, default=None, help='Slug тенанта (по умолчанию все)')

    def handle(self, *args, **options):
        domain = options['domain']
        if not has_tree(domain):
            self.stderr.write(self.style.ERROR(f'Нет вшитого дерева для домена «{domain}»'))
            return

        tenants = Tenant.objects.all()
        if options.get('tenant'):
            tenants = tenants.filter(slug=options['tenant'])

        importer = AvitoTreeImporter(domain)
        total = 0
        for tenant in tenants:
            created = importer.import_for_tenant(tenant)
            total += created
            self.stdout.write(f'  {tenant.slug}: +{created} категорий')
        self.stdout.write(self.style.SUCCESS(f'Готово: создано категорий {total}'))
