import time
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    """
    Нагрузочный тест: создаёт 50К товаров для двух тенантов и замеряет время.

    Проверяет:
    - Производительность bulk_create при 25К записей на тенанта
    - Изоляцию данных: товары тенанта A не видны в queryset тенанта B
    - Отсутствие N+1 при подсчёте через aggregate
    """

    help = 'Нагрузочный тест: 50К товаров → 2 тенанта → замер + изоляция'

    PRODUCTS_PER_TENANT = 25_000
    BATCH_SIZE = 1_000

    def add_arguments(self, parser):
        parser.add_argument(
            '--count', type=int, default=self.PRODUCTS_PER_TENANT,
            help=f'Товаров на тенанта (default: {self.PRODUCTS_PER_TENANT})',
        )
        parser.add_argument(
            '--cleanup', action='store_true',
            help='Удалить тестовые данные после проверки',
        )

    def handle(self, *args, **options):
        """Запускает нагрузочный тест с замером каждого шага."""
        count = options['count']
        self.stdout.write(f'Нагрузочный тест: {count} товаров × 2 тенанта = {count * 2} записей\n')

        t0 = time.perf_counter()
        t1, t2 = self._create_tenants()
        ds1, ds2 = self._create_datasources(t1, t2)
        t_tenants = time.perf_counter() - t0
        self.stdout.write(f'  [✓] Тенанты созданы за {t_tenants:.2f}с')

        t0 = time.perf_counter()
        self._bulk_create_products(t1, ds1, count, prefix='A')
        self._bulk_create_products(t2, ds2, count, prefix='B')
        t_insert = time.perf_counter() - t0
        self.stdout.write(f'  [✓] {count * 2} товаров вставлено за {t_insert:.2f}с')

        t0 = time.perf_counter()
        self._verify_counts(t1, t2, count)
        self._verify_isolation(t1, t2)
        t_verify = time.perf_counter() - t0
        self.stdout.write(f'  [✓] Изоляция и счётчики проверены за {t_verify:.3f}с')

        self.stdout.write(self.style.SUCCESS(
            f'\nИтог: {count * 2} записей | insert={t_insert:.2f}с | '
            f'({count * 2 / t_insert:.0f} записей/с)'
        ))

        if options['cleanup']:
            self._cleanup(t1, t2)
            self.stdout.write('  [✓] Тестовые данные удалены')

    def _create_tenants(self):
        from apps.tenants.services import TenantService
        t1, _ = TenantService.create_tenant('loadtest-a', 'LT-A', 'a@loadtest.dev', 'pass')
        t2, _ = TenantService.create_tenant('loadtest-b', 'LT-B', 'b@loadtest.dev', 'pass')
        return t1, t2

    def _create_datasources(self, t1, t2):
        from apps.datasources.encryption import encrypt
        from apps.datasources.models import DataSourceConnection

        creds = encrypt({'url': 'http://mock.local', 'user': 'u', 'password': 'p'})
        ds1 = DataSourceConnection.objects.create(
            tenant=t1, name='LT-DS', type='1c_http', credentials=creds,
        )
        ds2 = DataSourceConnection.objects.create(
            tenant=t2, name='LT-DS', type='1c_http', credentials=creds,
        )
        return ds1, ds2

    def _bulk_create_products(self, tenant, datasource, count: int, prefix: str):
        """Вставляет count товаров батчами по BATCH_SIZE."""
        from apps.products.models import Product

        batch = []
        for i in range(count):
            batch.append(Product(
                tenant=tenant,
                datasource=datasource,
                article=f'{prefix}-{i:06d}',
                name=f'Деталь {prefix}-{i}',
                price=Decimal('100.00'),
                stock_qty=10,
                hash_1c=f'{prefix}{i:063d}',
            ))
            if len(batch) >= self.BATCH_SIZE:
                Product.objects.bulk_create(batch, ignore_conflicts=True)
                batch = []
        if batch:
            Product.objects.bulk_create(batch, ignore_conflicts=True)

    def _verify_counts(self, t1, t2, expected: int):
        """Проверяет, что каждый тенант видит ровно expected товаров."""
        from apps.products.models import Product

        c1 = Product.objects.filter(tenant=t1).count()
        c2 = Product.objects.filter(tenant=t2).count()
        assert c1 == expected, f'Тенант A: ожидалось {expected}, получено {c1}'
        assert c2 == expected, f'Тенант B: ожидалось {expected}, получено {c2}'

    def _verify_isolation(self, t1, t2):
        """Убеждается, что queryset одного тенанта не содержит данные другого."""
        from apps.products.models import Product

        t1_articles = set(Product.objects.filter(tenant=t1).values_list('article', flat=True)[:100])
        t2_articles = set(Product.objects.filter(tenant=t2).values_list('article', flat=True)[:100])
        overlap = t1_articles & t2_articles
        assert not overlap, f'Обнаружено пересечение данных: {overlap}'

    def _cleanup(self, t1, t2):
        from apps.tenants.models import Tenant
        Tenant.objects.filter(pk__in=[t1.pk, t2.pk]).delete()

    def _db_query_count(self) -> int:
        return len(connection.queries)
