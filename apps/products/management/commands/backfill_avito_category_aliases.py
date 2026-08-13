from django.core.management.base import BaseCommand
from django.db import transaction

from apps.products.avito_category_aliases import avito_aliases_by_normalized_name
from apps.products.models import TenantCatalogCategory
from apps.products.part_category_seed import iter_base_part_categories, normalize_category_name
from apps.tenants.models import CatalogDomain

AVITO_SOURCE = 'avito'


class Command(BaseCommand):
    """Заполняет aliases у avito-категорий синонимами из старого ручного дерева
    (BASE_PART_CATEGORY_TREE) и курируемого словаря AVITO_CATEGORY_ALIASES.

    Авито не отдаёт синонимы в своём API категорий — там только name/slug.
    Без aliases авто-классификация товаров (infer_product_tenant_category)
    хуже находит нужную категорию по названию товара и чаще уходит в общий
    fallback «Автомобиль на запчасти». Алиасы применяются к одноимённым
    категориям в дереве Avito (по всем совпадающим веткам — для aliases
    неоднозначность имени не проблема, в отличие от переноса товаров).

    Идемпотентна: дозаполняет только недостающие синонимы, уже накопленные
    вручную алиасы не удаляет.
    """

    help = 'Заполнить aliases у avito-категорий синонимами старого дерева и курируемого словаря'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Только показать план, ничего не менять в БД',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        domain = CatalogDomain.objects.filter(slug='auto_parts').first()
        if domain is None:
            self.stderr.write(self.style.ERROR("Домен 'auto_parts' не найден"))
            return

        aliases_by_normalized_name: dict[str, list[str]] = {}
        for item in iter_base_part_categories():
            if not item['aliases']:
                continue
            key = normalize_category_name(item['name'])
            aliases_by_normalized_name.setdefault(key, list(item['aliases']))
        for key, aliases in avito_aliases_by_normalized_name().items():
            merged = aliases_by_normalized_name.setdefault(key, [])
            merged.extend(alias for alias in aliases if alias not in merged)

        updated = 0
        with transaction.atomic():
            categories = TenantCatalogCategory.objects.filter(
                root_domain=domain, external_source=AVITO_SOURCE,
            )
            for category in categories:
                category_aliases = aliases_by_normalized_name.get(category.normalized_name)
                if not category_aliases:
                    continue
                missing = [
                    alias for alias in category_aliases
                    if alias not in category.aliases
                ]
                if not missing:
                    continue
                updated += 1
                self.stdout.write(
                    f'  tenant={category.tenant_id} "{category.name}" += aliases={missing}'
                )
                if not dry_run:
                    category.aliases = [*category.aliases, *missing]
                    category.save(update_fields=['aliases'])

            if dry_run:
                transaction.set_rollback(True)

        verb = 'будет обновлено' if dry_run else 'обновлено'
        self.stdout.write(self.style.SUCCESS(f'Категорий, у которых {verb} aliases: {updated}'))
