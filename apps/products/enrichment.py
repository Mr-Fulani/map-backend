import hashlib
import re


PART_CODE_ALLOWED_RE = re.compile(r'[^0-9A-ZА-ЯЁ]+')


def normalize_part_code(value: str) -> str:
    """Нормализует артикул/OEM без потери ведущих нулей."""
    return PART_CODE_ALLOWED_RE.sub('', (value or '').upper())


def make_value_hash(value: str) -> str:
    """Стабильный hash для unique constraints на длинных текстовых значениях."""
    return hashlib.sha256((value or '').strip().encode()).hexdigest()
