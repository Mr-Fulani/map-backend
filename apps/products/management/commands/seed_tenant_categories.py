from django.core.management.base import BaseCommand

from apps.products.catalog_category_seed import BASE_CATEGORY_TEMPLATE_TREE
from apps.products.services import ProductCategorySeedService
from apps.tenants.models import CatalogDomain, Tenant, TenantCatalogDomain


class Command(BaseCommand):
    """Засевает приоритетные категории для всех тенантов и доступных доменов.

    Идемпотентна: использует get_or_create, существующие записи не изменяет.
    Не перезаписывает настроенные пользователем категории и не включает
    домены, которые пользователь явно отключил. Для автозапчастей использует
    каноническое дерево Avito, а для остальных доменов — базовые шаблоны.
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
        domains_by_slug = {
            d.slug: d
            for d in CatalogDomain.objects.filter(slug__in=domain_slugs, is_active=True)
        }

        total_created = 0
        for tenant in tenants:
            for slug in domain_slugs:
                domain = domains_by_slug.get(slug)
                if domain is None:
                    continue

                # Создаём TenantCatalogDomain только если его ещё нет.
                # Если пользователь уже включил или выключил домен — не трогаем.
                _, domain_created = TenantCatalogDomain.objects.get_or_create(
                    tenant=tenant,
                    domain=domain,
                    defaults={'is_enabled': True},
                )

                # Сеем категории только для включённых доменов.
                # get_or_create внутри не перезаписывает существующие записи.
                tenant_domain = TenantCatalogDomain.objects.filter(
                    tenant=tenant, domain=domain, is_enabled=True,
                ).first()
                if tenant_domain is None:
                    continue

                count = ProductCategorySeedService.seed_tenant_primary_categories(tenant, domain)
                if count:
                    self.stdout.write(
                        self.style.SUCCESS(f'  {tenant.slug} / {slug}: +{count} категорий')
                    )
                    total_created += count

        self.stdout.write(self.style.SUCCESS(f'Готово. Создано категорий: {total_created}'))
