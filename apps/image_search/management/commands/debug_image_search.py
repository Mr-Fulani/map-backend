"""Диагностика поиска изображений для конкретного товара."""

from django.core.management.base import BaseCommand, CommandError
from django.utils.timezone import now


class Command(BaseCommand):
    """Показывает кеш и логи поиска изображений для товара."""

    help = 'Диагностика поиска изображений: кеш и логи для товара по ID'

    def add_arguments(self, parser):
        parser.add_argument('product_id', type=int, help='ID товара')
        parser.add_argument(
            '--clear-cache',
            action='store_true',
            help='Очистить кеш для этого товара',
        )

    def handle(self, *args, **options):
        from apps.image_search.models import ImageSearchCache, ImageSearchLog
        from apps.products.models import Product

        pid = options['product_id']

        try:
            product = Product.objects.get(pk=pid)
        except Product.DoesNotExist:
            raise CommandError(f'Товар с id={pid} не найден')

        self.stdout.write(f'Товар: [{product.pk}] {product.brand} / {product.article} — {product.name[:60]}')
        self.stdout.write('')

        cache_key = f'img_search:{product.article}:{product.brand}'
        self.stdout.write(f'Cache key: {cache_key}')

        cache = ImageSearchCache.objects.filter(cache_key=cache_key).first()
        if cache:
            expired = cache.expires_at < now()
            status = 'ИСТЁК' if expired else 'АКТИВЕН'
            self.stdout.write(f'Кеш: {status}  expires={cache.expires_at}')
            self.stdout.write(f'  results={cache.results}')
            if options['clear_cache']:
                cache.delete()
                self.stdout.write(self.style.SUCCESS('Кеш удалён.'))
        else:
            self.stdout.write('Кеш: ОТСУТСТВУЕТ')

        self.stdout.write('')
        self.stdout.write('Последние 15 логов поиска:')
        self.stdout.write(f'  {"Время":<7} {"Источник":<15} {"Найдено":<9} {"Принято":<9} Ошибка')
        self.stdout.write('  ' + '-' * 70)

        logs = ImageSearchLog.objects.filter(product=product).order_by('-created_at')[:15]
        if not logs:
            self.stdout.write('  (логов нет)')
        for log in logs:
            err = (log.error or '')[:50]
            self.stdout.write(
                f'  {log.created_at.strftime("%H:%M:%S"):<7} '
                f'{log.source_id:<15} '
                f'{log.results_count:<9} '
                f'{log.accepted_count:<9} '
                f'{err}'
            )
