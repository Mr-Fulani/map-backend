import json

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings


def _fernet() -> MultiFernet:
    """Шифрует primary-ключом и расшифровывает также ключами ротации."""
    keys = getattr(settings, 'FIELD_ENCRYPTION_KEYS', None) or [
        settings.FIELD_ENCRYPTION_KEY,
    ]
    keys = [key.strip() for key in keys if key and key.strip()]
    if not keys:
        raise RuntimeError('FIELD_ENCRYPTION_KEY(S) не настроен.')
    return MultiFernet([Fernet(key.encode()) for key in keys])


def encrypt(data: dict) -> bytes:
    """Шифрует словарь в байты через Fernet. Хранить результат в BinaryField."""
    return _fernet().encrypt(json.dumps(data).encode())


def decrypt(data: bytes) -> dict:
    """Расшифровывает байты обратно в словарь."""
    return json.loads(_fernet().decrypt(bytes(data)).decode())


def encrypt_text(value: str) -> bytes:
    """Шифрует строковый секрет."""
    return _fernet().encrypt(value.encode())


def decrypt_text(value: bytes) -> str:
    """Расшифровывает строковый секрет."""
    return _fernet().decrypt(bytes(value)).decode()
