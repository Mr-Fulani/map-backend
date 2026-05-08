from django.shortcuts import get_object_or_404

from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.datasources.registry import get_adapter


class ConnectionService:
    @staticmethod
    def create(tenant, data: dict) -> DataSourceConnection:
        credentials = data.pop('credentials')
        return DataSourceConnection.objects.create(
            tenant=tenant,
            credentials=encrypt(credentials),
            **data,
        )

    @staticmethod
    def update(connection_id: int, tenant, data: dict) -> DataSourceConnection:
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
        get_object_or_404(DataSourceConnection, pk=connection_id, tenant=tenant).delete()

    @staticmethod
    def test(connection_id: int, tenant) -> dict:
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
