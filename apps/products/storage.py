import hashlib
import io
import re

import requests
from PIL import Image, UnidentifiedImageError
from django.conf import settings

from apps.products.models import ProductImage

MAX_PHOTOS = 10
MAX_DIMENSION = 1280
THUMB_DIMENSION = 400
DOWNLOAD_TIMEOUT = 15


def _key_part(value: object, fallback: str) -> str:
    translit_map = str.maketrans({
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
    })
    raw = str(value or '').strip().lower().translate(translit_map)
    raw = re.sub(r'\s+', '-', raw)
    raw = re.sub(r'[^0-9a-z_-]+', '', raw)
    raw = raw.strip('-_')
    return raw or fallback


def _product_media_base_key(product, sha: str) -> str:
    tenant_slug = _key_part(getattr(product.tenant, 'slug', '') or product.tenant_id, 'tenant')
    category = getattr(product, 'catalog_category', None)

    if category is not None:
        root_domain = getattr(category, 'root_domain', None)
        domain_slug = _key_part(
            getattr(root_domain, 'slug', '') or getattr(category, 'domain', ''),
            'unknown',
        )
        category_slug = _key_part(getattr(category, 'name', ''), 'uncategorized')
    else:
        classification = getattr(product, 'catalog_classification', None)
        domain_slug = _key_part(
            getattr(classification, 'domain', '') or getattr(product.tenant, 'catalog_domain', ''),
            'unknown',
        )
        category_slug = _key_part(getattr(product, 'category_1c', ''), 'uncategorized')

    brand = _key_part(getattr(product, 'brand', ''), 'brand')
    article = _key_part(getattr(product, 'article', ''), f'product-{product.pk}')
    filename = f'{category_slug}-{brand}-{article}-{sha[:12]}'
    media_key = f'products/{tenant_slug}/{domain_slug}/{category_slug}/{filename}'
    prefix = str(getattr(settings, 'MEDIA_KEY_PREFIX', '') or '').strip('/')
    return f'{prefix}/{media_key}' if prefix else media_key


def _product_media_keys(product, sha: str) -> tuple[str, str]:
    base_key = _product_media_base_key(product, sha)
    return f'{base_key}.jpg', f'{base_key}_thumb.jpg'


def _resize(img: Image.Image, max_px: int) -> Image.Image:
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    return img


def _to_jpeg_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    rgb = img.convert('RGB')
    rgb.save(buf, format='JPEG', quality=85, optimize=True)
    return buf.getvalue()


class PhotoUploadPipeline:
    def __init__(self, storage=None):
        # storage — django-storages backend; в тестах подменяется mock
        if storage is None:
            from django.core.files.storage import default_storage
            self._storage = default_storage
        else:
            self._storage = storage

    def process(
        self, source_url: str, product, source_id: str = '',
        status: str | None = None,
        check_limit: bool = False,
    ) -> ProductImage | None:
        if check_limit and product.images.exclude(status=ProductImage.Status.REJECTED).count() >= MAX_PHOTOS:
            return None

        try:
            response = requests.get(source_url, timeout=DOWNLOAD_TIMEOUT)
            response.raise_for_status()
            raw = response.content
        except requests.RequestException:
            return None

        sha = hashlib.sha256(raw).hexdigest()
        existing = product.images.filter(sha256=sha).first()

        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except (UnidentifiedImageError, Exception):
            return None

        original_bytes = _to_jpeg_bytes(_resize(img.copy(), MAX_DIMENSION))
        thumb_bytes = _to_jpeg_bytes(_resize(img.copy(), THUMB_DIMENSION))

        s3_key, s3_key_thumb = _product_media_keys(product, sha)

        try:
            self._storage.save(s3_key, io.BytesIO(original_bytes))
            self._storage.save(s3_key_thumb, io.BytesIO(thumb_bytes))
        except Exception:
            return None

        if existing is not None:
            update_fields = []
            if existing.s3_key != s3_key:
                existing.s3_key = s3_key
                update_fields.append('s3_key')
            if existing.s3_key_thumb != s3_key_thumb:
                existing.s3_key_thumb = s3_key_thumb
                update_fields.append('s3_key_thumb')
            if source_url and existing.url_source != source_url:
                existing.url_source = source_url
                update_fields.append('url_source')
            if source_id and existing.source_id != source_id:
                existing.source_id = source_id
                update_fields.append('source_id')
            if update_fields:
                existing.save(update_fields=update_fields)
            return existing

        position = product.images.count()
        return ProductImage.objects.create(
            product=product,
            s3_key=s3_key,
            s3_key_thumb=s3_key_thumb,
            url_source=source_url,
            sha256=sha,
            position=position,
            source_id=source_id,
            status=status or ProductImage.Status.IMPORTED,
        )
