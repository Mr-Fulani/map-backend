"""Management command для проверки подключения к S3.

Загружает тестовый файл, читает его, удаляет.
Используется для верификации настроек YC S3 перед запуском image_search.
"""

import io

from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Проверяет подключение к S3 (Yandex Cloud): загрузка, чтение, удаление тестового файла.'

    def handle(self, *args, **options):
        test_key = 'products/test/_s3_connection_test.txt'
        test_content = b'MAP S3 connection test'

        self.stdout.write('1. Загрузка тестового файла...')
        try:
            default_storage.save(test_key, io.BytesIO(test_content))
            self.stdout.write(self.style.SUCCESS(f'   OK: {test_key}'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'   ОШИБКА загрузки: {e}'))
            return

        self.stdout.write('2. Проверка существования...')
        if default_storage.exists(test_key):
            self.stdout.write(self.style.SUCCESS('   OK: файл существует'))
        else:
            self.stdout.write(self.style.ERROR('   ОШИБКА: файл не найден после загрузки'))
            return

        self.stdout.write('3. Чтение файла...')
        try:
            with default_storage.open(test_key, 'rb') as f:
                content = f.read()
            if content == test_content:
                self.stdout.write(self.style.SUCCESS('   OK: содержимое совпадает'))
            else:
                self.stdout.write(self.style.WARNING(f'   ПРЕДУПРЕЖДЕНИЕ: содержимое отличается'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'   ОШИБКА чтения: {e}'))

        self.stdout.write('4. Получение URL...')
        try:
            url = default_storage.url(test_key)
            self.stdout.write(self.style.SUCCESS(f'   URL: {url}'))
        except Exception as e:
            self.stdout.write(self.style.WARNING(f'   URL недоступен: {e}'))

        self.stdout.write('5. Удаление тестового файла...')
        try:
            default_storage.delete(test_key)
            self.stdout.write(self.style.SUCCESS('   OK: файл удалён'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'   ОШИБКА удаления: {e}'))

        self.stdout.write(self.style.SUCCESS('\n✅ S3 подключение работает корректно!'))
