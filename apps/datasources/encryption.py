import json

from cryptography.fernet import Fernet
from django.conf import settings


def _fernet() -> Fernet:
    """Создаёт экземпляр Fernet с ключом из настроек."""
    return Fernet(settings.FIELD_ENCRYPTION_KEY.encode())


def encrypt(data: dict) -> bytes:
    """Шифрует словарь в байты через Fernet. Хранить результат в BinaryField."""
    return _fernet().encrypt(json.dumps(data).encode())


def decrypt(data: bytes) -> dict:
    """Расшифровывает байты обратно в словарь."""
    return json.loads(_fernet().decrypt(bytes(data)).decode())
