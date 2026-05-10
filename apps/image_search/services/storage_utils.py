"""Утилиты для хранения изображений: SEO-имена, пути в S3, resize.

Переиспользует _resize и _to_jpeg_bytes из apps/products/storage.py.
"""

import hashlib
import re

try:
    from transliterate import translit
except ImportError:
    translit = None


def slugify_ru(text: str, max_words: int | None = None) -> str:
    """Транслитерация + slug.

    Примеры:
        'HYUNDAI-KIA' → 'hyundai-kia'
        'Горловина заливная' → 'gorlovyna-zalivnaya'
    """
    if not text:
        return 'unknown'
    if translit is not None:
        try:
            text = translit(text, 'ru', reversed=True)
        except Exception:
            pass  # уже латиница
    text = re.sub(r'[^a-zA-Z0-9\s-]', '', text.lower())
    text = re.sub(r'[\s_]+', '-', text).strip('-')
    if max_words:
        text = '-'.join(text.split('-')[:max_words])
    return text or 'unknown'


def build_seo_filename(product, index: int, ext: str = 'jpg') -> str:
    """Генерирует SEO-дружелюбное имя файла.

    Результат: hyundai-kia-25327h5010-gorlovyna-zalivnaya-radiatora-a1b2c3d4.jpg

    Args:
        product: экземпляр Product (поля brand, article, name).
        index: порядковый номер изображения.
        ext: расширение файла.
    """
    brand = slugify_ru(product.brand)
    article = slugify_ru(product.article or 'no-article')
    desc = slugify_ru(product.name, max_words=5)
    uid = hashlib.md5(f'{product.pk}-{index}'.encode()).hexdigest()[:8]
    return f'{brand}-{article}-{desc}-{uid}.{ext}'


def build_s3_path(product, filename: str) -> str:
    """Генерирует путь в S3 для файла изображения.

    Результат: products/hyundai-kia/25327h5010/filename.jpg

    Args:
        product: экземпляр Product (поля brand, article).
        filename: имя файла (из build_seo_filename).
    """
    brand = slugify_ru(product.brand)
    article = slugify_ru(product.article or 'no-article')
    return f'products/{brand}/{article}/{filename}'
