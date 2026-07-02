from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.products.models import Product, TenantCatalogCategory
from apps.tenants.models import CatalogDomain

SEED_SOURCE = 'platform_auto_parts_seed'
AVITO_SOURCE = 'avito'


class Command(BaseCommand):
    """Удаляет устаревшее ручное дерево категорий 'Автозапчасти' (platform_auto_parts_seed),
    оставляя только официальное дерево Avito (external_source='avito').

    Перед удалением товары со старой категорией переносятся на avito-категорию
    с тем же нормализованным именем — но только если совпадение однозначно
    (в дереве avito нет другой ветки с тем же именем для другого типа техники).
    Остальные товары остаются без категории — тенант назначит категорию вручную
    через массовые действия в дашборде.

    Не трогает категории с другим external_source (в т.ч. созданные тенантом
    вручную) и другие домены каталога.
    """

    help = "Удалить устаревшее ручное дерево 'Автозапчасти', перенеся товары на дерево Avito где однозначно"

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

        seed_categories = list(
            TenantCatalogCategory.objects.filter(root_domain=domain, external_source=SEED_SOURCE)
        )
        if not seed_categories:
            self.stdout.write('Нечего удалять — категорий platform_auto_parts_seed нет.')
            return

        # avito-категории по (tenant_id, normalized_name) — считаем количество веток
        # с одинаковым именем, чтобы не переносить в неоднозначных случаях.
        avito_by_key = defaultdict(list)
        for cat in TenantCatalogCategory.objects.filter(root_domain=domain, external_source=AVITO_SOURCE):
            avito_by_key[(cat.tenant_id, cat.normalized_name)].append(cat.id)

        remapped_categories = 0
        remapped_products = 0
        ambiguous_categories = 0
        ambiguous_products = 0
        unmatched_categories = 0
        unmatched_products = 0

        with transaction.atomic():
            for seed_cat in seed_categories:
                candidates = avito_by_key.get((seed_cat.tenant_id, seed_cat.normalized_name), [])
                products_qs = Product.objects.filter(catalog_category=seed_cat)
                products_count = products_qs.count()

                if len(candidates) == 1:
                    remapped_categories += 1
                    remapped_products += products_count
                    self.stdout.write(
                        f'  [remap] tenant={seed_cat.tenant_id} "{seed_cat.name}" '
                        f'({products_count} товаров) -> avito#{candidates[0]}'
                    )
                    if not dry_run and products_count:
                        products_qs.update(catalog_category_id=candidates[0])
                elif len(candidates) > 1:
                    ambiguous_categories += 1
                    ambiguous_products += products_count
                    self.stdout.write(
                        f'  [ambiguous] tenant={seed_cat.tenant_id} "{seed_cat.name}" '
                        f'({products_count} товаров) — {len(candidates)} веток avito с таким именем, '
                        'оставляем без категории'
                    )
                else:
                    unmatched_categories += 1
                    unmatched_products += products_count

            self.stdout.write(
                f'  [no match] {unmatched_categories} категорий без аналога в avito '
                f'({unmatched_products} товаров) — останутся без категории'
            )

            deleted_count = TenantCatalogCategory.objects.filter(
                root_domain=domain, external_source=SEED_SOURCE,
            ).count()

            if dry_run:
                self.stdout.write(self.style.WARNING(f'DRY RUN: было бы удалено {deleted_count} категорий'))
                transaction.set_rollback(True)
            else:
                TenantCatalogCategory.objects.filter(
                    root_domain=domain, external_source=SEED_SOURCE,
                ).delete()
                self.stdout.write(self.style.SUCCESS(f'Удалено категорий: {deleted_count}'))

        self.stdout.write(self.style.SUCCESS(
            f'Итог: перенесено {remapped_products} товаров ({remapped_categories} категорий), '
            f'неоднозначных {ambiguous_products} товаров ({ambiguous_categories} категорий), '
            f'без аналога {unmatched_products} товаров ({unmatched_categories} категорий)'
        ))
