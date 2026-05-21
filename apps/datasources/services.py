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
    def process_csv_upload(tenant, file_name: str, items: list[dict]) -> dict:
        """Сохраняет загруженные из CSV товары и обновляет подключение."""
        from apps.products.services import ProductService
        from django.utils.timezone import now

        connection, _ = DataSourceConnection.objects.get_or_create(
            tenant=tenant,
            type=DataSourceConnection.TYPE_CSV,
            defaults={'name': file_name}
        )

        counts = {'created': 0, 'updated': 0, 'unchanged': 0}
        for item in items:
            _, status_res = ProductService.upsert_from_source(tenant, connection, item)
            counts[status_res] += 1

        connection.last_sync_at = now()
        connection.last_sync_status = DataSourceConnection.STATUS_OK
        connection.save(update_fields=['last_sync_at', 'last_sync_status'])

        return {
            'id': connection.id,
            'rows_count': len(items),
            'counts': counts,
        }
