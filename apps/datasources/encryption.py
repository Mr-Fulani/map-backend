import json

from cryptography.fernet import Fernet
from django.conf import settings


def _fernet() -> Fernet:
    return Fernet(settings.FIELD_ENCRYPTION_KEY.encode())


def encrypt(data: dict) -> bytes:
    return _fernet().encrypt(json.dumps(data).encode())


def decrypt(data: bytes) -> dict:
    return json.loads(_fernet().decrypt(bytes(data)).decode())
