import hashlib
import io

import requests
from PIL import Image, UnidentifiedImageError

from apps.products.models import ProductImage

MAX_PHOTOS = 10
MAX_DIMENSION = 1280
THUMB_DIMENSION = 400
DOWNLOAD_TIMEOUT = 15


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

    def process(self, source_url: str, product) -> ProductImage | None:
        if product.images.count() >= MAX_PHOTOS:
            return None

        try:
            response = requests.get(source_url, timeout=DOWNLOAD_TIMEOUT)
            response.raise_for_status()
            raw = response.content
        except requests.RequestException:
            return None

        sha = hashlib.sha256(raw).hexdigest()
        if product.images.filter(sha256=sha).exists():
            return product.images.get(sha256=sha)

        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except (UnidentifiedImageError, Exception):
            return None

        original_bytes = _to_jpeg_bytes(_resize(img.copy(), MAX_DIMENSION))
        thumb_bytes = _to_jpeg_bytes(_resize(img.copy(), THUMB_DIMENSION))

        base_key = f'products/{product.tenant_id}/{product.pk}/{sha}'
        s3_key = f'{base_key}.jpg'
        s3_key_thumb = f'{base_key}_thumb.jpg'

        self._storage.save(s3_key, io.BytesIO(original_bytes))
        self._storage.save(s3_key_thumb, io.BytesIO(thumb_bytes))

        position = product.images.count()
        return ProductImage.objects.create(
            product=product,
            s3_key=s3_key,
            s3_key_thumb=s3_key_thumb,
            url_source=source_url,
            sha256=sha,
            position=position,
        )
