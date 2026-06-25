from django.shortcuts import get_object_or_404

from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.datasources.registry import get_adapter


class ConnectionService:
    """Сервис управления подключениями к источникам данных."""

    @staticmethod
    def create(tenant, data: dict) -> DataSourceConnection:
        """Создаёт подключение с зашифрованными credentials."""
        credentials = data.pop('credentials')
        return DataSourceConnection.objects.create(
            tenant=tenant,
            credentials=encrypt(credentials),
            **data,
        )

    @staticmethod
    def update(connection_id: int, tenant, data: dict) -> DataSourceConnection:
        """Обновляет поля подключения. Credentials шифруются если переданы."""
        conn = get_object_or_404(DataSourceConnection, pk=connection_id, tenant=tenant)
        credentials = data.pop('credentials', None)
        if credentials is not None:
            conn.credentials = encrypt(credentials)
        for field, value in data.items():
            setattr(conn, field, value)
        update_fields = list(data.keys())
        if credentials is not None:
            update_fields.append('credentials')
        conn.save(update_fields=update_fields)
        return conn

    @staticmethod
    def delete(connection_id: int, tenant) -> None:
        """Удаляет подключение тенанта."""
        get_object_or_404(DataSourceConnection, pk=connection_id, tenant=tenant).delete()

    @staticmethod
    def test(connection_id: int, tenant) -> dict:
        """Проверяет доступность источника и обновляет last_sync_status."""
        conn = get_object_or_404(DataSourceConnection, pk=connection_id, tenant=tenant)
        try:
            adapter = get_adapter(conn)
            ok = adapter.test_connection()
            conn.last_sync_status = DataSourceConnection.STATUS_OK
            conn.last_error = ''
            conn.save(update_fields=['last_sync_status', 'last_error'])
            return {'ok': ok}
        except Exception as exc:
            conn.last_sync_status = DataSourceConnection.STATUS_ERROR
            conn.last_error = str(exc)
            conn.save(update_fields=['last_sync_status', 'last_error'])
            return {'ok': False, 'error': str(exc)}

    @staticmethod
    def find_duplicate_upload(tenant, content_hash: str, file_name: str) -> dict | None:
        """
        Ищет ранее загруженный тем же тенантом CSV-файл с тем же содержимым или именем.

        Нужно, чтобы предупредить о повторной загрузке и не плодить дубли товаров.
        Возвращает данные о найденном источнике или None, если совпадений нет.
        """
        qs = DataSourceConnection.objects.filter(
            tenant=tenant, type=DataSourceConnection.TYPE_CSV,
        )
        match = None
        reason = None
        if content_hash:
            match = qs.filter(content_hash=content_hash).order_by('-created_at').first()
            reason = 'hash'
        if match is None:
            match = qs.filter(name=file_name).order_by('-created_at').first()
            reason = 'name'
        if match is None:
            return None

        uploaded_at = match.created_at.strftime('%d.%m.%Y %H:%M')
        if reason == 'hash':
            message = f'Файл с таким же содержимым уже загружали {uploaded_at}. Загрузить повторно?'
        else:
            message = f'Файл с именем «{file_name}» уже загружали {uploaded_at}. Загрузить повторно?'
        return {
            'reason': reason,
            'connection_id': match.id,
            'uploaded_at': match.created_at.isoformat(),
            'message': message,
        }

    @staticmethod
    def process_csv_upload(tenant, file_name: str, items: list[dict], content_hash: str = '') -> dict:
        """Сохраняет загруженные из CSV товары и обновляет подключение."""
        from apps.products.services import ProductService
        from django.utils.timezone import now

        connection = DataSourceConnection.objects.create(
            tenant=tenant,
            type=DataSourceConnection.TYPE_CSV,
            name=file_name,
            credentials=encrypt({'file_name': file_name}),
            content_hash=content_hash,
        )

        counts = {'created': 0, 'updated': 0, 'unchanged': 0}
        for item in items:
            _, status_res, _ = ProductService.upsert_from_source(tenant, connection, item)
            counts[status_res] += 1

        connection.last_sync_at = now()
        connection.last_sync_status = DataSourceConnection.STATUS_OK
        connection.save(update_fields=['last_sync_at', 'last_sync_status'])

        # Авто-классификация (домен + категория) загруженных товаров — как и при
        # 1С-импорте. Без неё категория не определяется и маппинг на Avito не
        # работает. Асинхронно, чтобы не блокировать ответ на загрузку файла.
        if counts['created'] or counts['updated']:
            from apps.products.tasks import classify_tenant_products
            classify_tenant_products.delay(tenant.id)

        return {
            'id': connection.id,
            'rows_count': len(items),
            'counts': counts,
        }
