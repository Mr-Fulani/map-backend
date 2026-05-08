import pytest

from apps.datasources.encryption import decrypt, encrypt


@pytest.mark.django_db
class TestEncryption:
    def test_credentials_round_trip(self):
        data = {'url': 'http://1c.example.com', 'user': 'admin', 'password': 'secret'}
        encrypted = encrypt(data)
        assert decrypt(encrypted) == data

    def test_credentials_stored_encrypted(self):
        """Зашифрованные данные не содержат открытый текст пароля."""
        data = {'user': 'admin', 'password': 'supersecret'}
        encrypted = encrypt(data)
        assert b'supersecret' not in bytes(encrypted)

    def test_plaintext_not_in_db(self, db):
        """В БД хранится бинарное зашифрованное значение, не строка."""
        from apps.datasources.models import DataSourceConnection
        from apps.tenants.services import TenantService

        tenant, _ = TenantService.create_tenant('enc-test', 'enc-test', 'enc@test.com', 'pass12345')
        creds = {'url': 'http://example.com', 'user': 'u', 'password': 'topsecret'}
        conn = DataSourceConnection.objects.create(
            tenant=tenant,
            name='Test',
            type='1c_http',
            credentials=encrypt(creds),
        )
        from django.db import connection as db_conn
        with db_conn.cursor() as cur:
            cur.execute('SELECT credentials FROM datasources_datasourceconnection WHERE id = %s', [conn.pk])
            raw = cur.fetchone()[0]
        assert b'topsecret' not in (raw if isinstance(raw, bytes) else raw.tobytes())
