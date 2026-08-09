from io import StringIO

import pytest
from cryptography.fernet import Fernet
from django.core.management import call_command

from apps.datasources.encryption import decrypt
from apps.web_research.models import WebSearchConnection


@pytest.mark.django_db
def test_rotation_includes_web_search_credentials_and_dry_run_is_read_only(settings):
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    settings.FIELD_ENCRYPTION_KEY = old_key
    settings.FIELD_ENCRYPTION_KEYS = [old_key]
    connection = WebSearchConnection(
        provider_id='rotation-test',
        display_name='Rotation test',
    )
    connection.set_credentials({'api_key': 'secret-value'})
    connection.save()
    old_ciphertext = bytes(connection.credentials_enc)

    settings.FIELD_ENCRYPTION_KEY = new_key
    settings.FIELD_ENCRYPTION_KEYS = [new_key, old_key]
    output = StringIO()
    call_command('rotate_encryption_keys', '--dry-run', stdout=output)

    connection.refresh_from_db()
    assert bytes(connection.credentials_enc) == old_ciphertext
    assert '[dry-run] web_search_connections: 1' in output.getvalue()

    call_command('rotate_encryption_keys', stdout=StringIO())

    connection.refresh_from_db()
    assert bytes(connection.credentials_enc) != old_ciphertext
    settings.FIELD_ENCRYPTION_KEYS = [new_key]
    assert decrypt(connection.credentials_enc) == {'api_key': 'secret-value'}
