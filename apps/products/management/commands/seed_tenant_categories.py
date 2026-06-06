from django.core.management.base import BaseCommand

from apps.products.catalog_category_seed import BASE_CATEGORY_TEMPLATE_TREE
from apps.products.services import ProductCategorySeedService
from apps.tenants.models import Tenant


class Command(BaseCommand):
    """Засевает категории каталога для всех тенантов по всем включённым доменам.

    Используется для первоначального наполнения продакшн БД после деплоя миграций
    и для починки тенантов, созданных когда домен был 'unknown'.
    """

    help = 'Seed catalog categories for all tenants across all available domains'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant',
            type=str,
            help='Slug конкретного тенанта (по умолчанию — все)',
        )
        parser.add_argument(
            '--domain',
            type=str,
            help='Slug конкретного домена (по умолчанию — все из BASE_CATEGORY_TEMPLATE_TREE)',
        )

    def handle(self, *args, **options):
        tenant_slug = options.get('tenant')
        domain_slug = options.get('domain')

        tenants = Tenant.objects.all()
        if tenant_slug:
            tenants = tenants.filter(slug=tenant_slug)
            if not tenants.exists():
                self.stderr.write(self.style.ERROR(f'Тенант {tenant_slug!r} не найден'))
                return

        domain_slugs = [domain_slug] if domain_slug else list(BASE_CATEGORY_TEMPLATE_TREE.keys())

        total_created = 0
        for tenant in tenants:
            for slug in domain_slugs:
                count = ProductCategorySeedService.enable_tenant_catalog_domain(tenant, slug)
                if count:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'  {tenant.slug} / {slug}: +{count} категорий'
                        )
                    )
                    total_created += count

        self.stdout.write(self.style.SUCCESS(f'Готово. Создано категорий: {total_created}'))
