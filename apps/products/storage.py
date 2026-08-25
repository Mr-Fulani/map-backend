import hashlib
import io
import logging
import re
import uuid
from urllib.parse import urlparse

import requests
from PIL import Image, UnidentifiedImageError
from django.conf import settings
from django.db import IntegrityError

from apps.core.image_security import validate_image_pixel_budget
from apps.core.storage import delete_storage_keys
from apps.core.url_security import request_public_http_url
from apps.products.media import PUBLISHABLE_IMAGE_STATUSES
from apps.products.models import ProductImage

MAX_PHOTOS = 10
MAX_DIMENSION = 1280
THUMB_DIMENSION = 400
DOWNLOAD_TIMEOUT = 15
# Макс. расстояние Хэмминга между aHash, при котором изображения считаем одним
# (одна фотография из разных источников: tachka/rossko/поиск). 0 — идентичны.
PERCEPTUAL_DUP_DISTANCE = 6

_BROWSER_IMAGE_ACCEPT = 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8'
_BROWSER_USER_AGENT = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
)

logger = logging.getLogger(__name__)


def source_image_request_headers(source_url: str, source_id: str = '') -> dict[str, str]:
    """Возвращает безопасные заголовки, необходимые CDN конкретного каталога."""
    hostname = (urlparse(str(source_url or '')).hostname or '').lower()
    if source_id.lower() == 'rossko' or hostname == 'imgs.rossko.ru':
        return {
            'User-Agent': _BROWSER_USER_AGENT,
            'Accept': _BROWSER_IMAGE_ACCEPT,
            'Referer': 'https://rossko.ru/',
        }
    return {}


def perceptual_hash(img: Image.Image) -> str:
    """64-битный average hash (aHash) в hex — для дедупа похожих изображений."""
    try:
        small = img.convert('L').resize((8, 8), Image.Resampling.LANCZOS)
        pixels = list(small.tobytes())
        avg = sum(pixels) / len(pixels)
        bits = 0
        for pixel in pixels:
            bits = (bits << 1) | (1 if pixel >= avg else 0)
        return f'{bits:016x}'
    except Exception:
        return ''


def phash_distance(left: str, right: str) -> int:
    """Расстояние Хэмминга между двумя aHash; 64 если один пустой/некорректный."""
    if not left or not right or len(left) != len(right):
        return 64
    try:
        return bin(int(left, 16) ^ int(right, 16)).count('1')
    except ValueError:
        return 64


def find_perceptual_duplicate(product, phash: str) -> ProductImage | None:
    """Существующее не-отклонённое фото товара, перцептивно совпадающее с phash."""
    if not phash:
        return None
    for other in product.images.exclude(status=ProductImage.Status.REJECTED).exclude(phash=''):
        if phash_distance(other.phash, phash) <= PERCEPTUAL_DUP_DISTANCE:
            return other
    return None


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
    return media_storage_key(media_key)


def media_storage_key(key: str) -> str:
    """Apply the environment namespace to an application-owned media key."""
    media_key = str(key).strip('/')
    prefix = str(getattr(settings, 'MEDIA_KEY_PREFIX', '') or '').strip('/')
    return f'{prefix}/{media_key}' if prefix else media_key


def _product_media_keys(product, sha: str) -> tuple[str, str]:
    base_key = _product_media_base_key(product, sha)
    return f'{base_key}.jpg', f'{base_key}_thumb.jpg'


def _save_product_image_pair(
    storage,
    original_key: str,
    original_bytes: bytes,
    thumb_key: str,
    thumb_bytes: bytes,
) -> tuple[str, str] | None:
    """Persist an original/thumb pair and return the actual backend keys.

    ``Storage.save`` may suffix a name when overwrite is disabled. The returned
    names are therefore the only keys that may be written to the database.
    """
    saved_keys: list[str] = []
    try:
        saved_original_key = storage.save(original_key, io.BytesIO(original_bytes))
        saved_keys.append(saved_original_key)
        saved_thumb_key = storage.save(thumb_key, io.BytesIO(thumb_bytes))
        saved_keys.append(saved_thumb_key)
    except Exception:
        delete_storage_keys(saved_keys, storage=storage)
        logger.warning('Failed to persist product image pair', exc_info=True)
        return None
    return saved_original_key, saved_thumb_key


def _update_existing_image_metadata(
    existing: ProductImage,
    *,
    source_url: str,
    source_id: str,
    phash: str,
    actual_width: int,
    actual_height: int,
    raw_size: int,
) -> ProductImage:
    update_fields = []
    if source_url and existing.url_source != source_url:
        existing.url_source = source_url
        update_fields.append('url_source')
    if source_id and existing.source_id != source_id:
        existing.source_id = source_id
        update_fields.append('source_id')
    if phash and not existing.phash:
        existing.phash = phash
        update_fields.append('phash')
    if existing.resolution_w != actual_width:
        existing.resolution_w = actual_width
        update_fields.append('resolution_w')
    if existing.resolution_h != actual_height:
        existing.resolution_h = actual_height
        update_fields.append('resolution_h')
    actual_size_kb = max(1, raw_size // 1024)
    if existing.file_size_kb != actual_size_kb:
        existing.file_size_kb = actual_size_kb
        update_fields.append('file_size_kb')
    if update_fields:
        existing.save(update_fields=update_fields)
    return existing


def _resize(img: Image.Image, max_px: int) -> Image.Image:
    img.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
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
        validate_quality: bool = False,
        allow_low_resolution: bool = False,
    ) -> ProductImage | None:
        if check_limit and product.images.exclude(status=ProductImage.Status.REJECTED).count() >= MAX_PHOTOS:
            return None

        try:
            request_headers = source_image_request_headers(source_url, source_id)
            response = request_public_http_url(
                source_url,
                timeout=DOWNLOAD_TIMEOUT,
                headers=request_headers,
                max_response_bytes=settings.MAX_IMAGE_UPLOAD_BYTES,
            )
            response.raise_for_status()
            raw = response.content
        except (requests.RequestException, ValueError):
            return None

        if validate_quality:
            content_type = str(response.headers.get('Content-Type', '')).split(';', 1)[0]
            if content_type and not content_type.startswith('image/'):
                return None

        sha = hashlib.sha256(raw).hexdigest()
        existing = product.images.filter(sha256=sha).first()

        try:
            img = Image.open(io.BytesIO(raw))
            validate_image_pixel_budget(img)
            img.load()
        except (UnidentifiedImageError, Exception):
            return None

        actual_width, actual_height = img.size
        if validate_quality:
            min_resolution = int(
                getattr(settings, 'IMAGE_SEARCH_SETTINGS', {}).get('MIN_RESOLUTION', 300),
            )
            if min(actual_width, actual_height) < min_resolution and not allow_low_resolution:
                return None
            if getattr(img, 'n_frames', 1) > 1:
                return None
            aspect_ratio = max(actual_width, actual_height) / min(actual_width, actual_height)
            if aspect_ratio > 5:
                return None

        # Дедуп по перцептивному хэшу: одна и та же фотография из разных источников
        # (tachka/rossko/поиск) имеет разные байты (sha не совпадёт), но aHash близок.
        # Возвращаем уже существующее фото, не плодим визуальные дубли в ревью.
        phash = perceptual_hash(img)
        if existing is None:
            duplicate = find_perceptual_duplicate(product, phash)
            if duplicate is not None:
                return duplicate

        original_bytes = _to_jpeg_bytes(_resize(img.copy(), MAX_DIMENSION))
        thumb_bytes = _to_jpeg_bytes(_resize(img.copy(), THUMB_DIMENSION))

        if existing is not None:
            return self._update_existing_with_fence(
                existing.pk,
                expected_sha=sha,
                source_url=source_url,
                source_id=source_id,
                phash=phash,
                actual_width=actual_width,
                actual_height=actual_height,
                raw_size=len(raw),
            )

        # Storage is external to the database transaction. Persist an
        # immutable attempt pair before taking any account/product/image row
        # locks; only the short CAS/create section below runs under the fence.
        requested_original_key, requested_thumb_key = _product_media_keys(product, sha)
        attempt_token = uuid.uuid4().hex
        original_stem, original_suffix = requested_original_key.rsplit('.', 1)
        thumb_stem, thumb_suffix = requested_thumb_key.rsplit('.', 1)
        thumb_base = thumb_stem.removesuffix('_thumb')
        saved_keys = _save_product_image_pair(
            self._storage,
            f'{original_stem}-auto-{attempt_token}.{original_suffix}',
            original_bytes,
            f'{thumb_base}-auto-{attempt_token}_thumb.{thumb_suffix}',
            thumb_bytes,
        )
        if saved_keys is None:
            return None
        saved_original_key, saved_thumb_key = saved_keys
        target_status = status or ProductImage.Status.IMPORTED

        class _ExistingImage(RuntimeError):
            def __init__(self, image_id: int):
                self.image_id = image_id

        class _PerceptualDuplicate(RuntimeError):
            def __init__(self, image_id: int):
                self.image_id = image_id

        class _LimitReached(RuntimeError):
            pass

        from apps.products.feed_writers import (
            StaleProductFeedWrite,
            locked_product_images_feed_write,
        )

        for _attempt in range(3):
            try:
                with locked_product_images_feed_write(
                    product.pk,
                    bump=target_status in PUBLISHABLE_IMAGE_STATUSES,
                ) as (locked_product, images):
                    same_sha = next(
                        (
                            candidate for candidate in images.values()
                            if candidate.sha256 == sha
                        ),
                        None,
                    )
                    if same_sha is not None:
                        # Roll back a conservative publishable-image bump. The
                        # existing row is updated in a separate no-bump fence
                        # after the prepared objects have been cleaned up.
                        raise _ExistingImage(same_sha.pk)

                    duplicate = next(
                        (
                            candidate for candidate in images.values()
                            if (
                                candidate.status != ProductImage.Status.REJECTED
                                and candidate.phash
                                and phash_distance(candidate.phash, phash)
                                <= PERCEPTUAL_DUP_DISTANCE
                            )
                        ),
                        None,
                    )
                    if duplicate is not None:
                        raise _PerceptualDuplicate(duplicate.pk)
                    if (
                        check_limit
                        and sum(
                            candidate.status != ProductImage.Status.REJECTED
                            for candidate in images.values()
                        ) >= MAX_PHOTOS
                    ):
                        raise _LimitReached

                    uploaded = ProductImage.objects.create(
                        product=locked_product,
                        s3_key=saved_original_key,
                        s3_key_thumb=saved_thumb_key,
                        url_source=source_url,
                        sha256=sha,
                        position=len(images),
                        source_id=source_id,
                        status=target_status,
                        phash=phash,
                        resolution_w=actual_width,
                        resolution_h=actual_height,
                        file_size_kb=max(1, len(raw) // 1024),
                    )
                return uploaded
            except (IntegrityError, StaleProductFeedWrite):
                continue
            except _ExistingImage as exc:
                delete_storage_keys(saved_keys, storage=self._storage)
                return self._update_existing_with_fence(
                    exc.image_id,
                    expected_sha=sha,
                    source_url=source_url,
                    source_id=source_id,
                    phash=phash,
                    actual_width=actual_width,
                    actual_height=actual_height,
                    raw_size=len(raw),
                )
            except _PerceptualDuplicate as exc:
                delete_storage_keys(saved_keys, storage=self._storage)
                return ProductImage.objects.filter(pk=exc.image_id).first()
            except _LimitReached:
                delete_storage_keys(saved_keys, storage=self._storage)
                return None
            except Exception:
                delete_storage_keys(saved_keys, storage=self._storage)
                raise

        delete_storage_keys(saved_keys, storage=self._storage)
        raise StaleProductFeedWrite(
            f'Product {product.pk} images changed repeatedly during upload.',
        )

    @staticmethod
    def _update_existing_with_fence(
        image_id: int,
        *,
        expected_sha: str,
        source_url: str,
        source_id: str,
        phash: str,
        actual_width: int,
        actual_height: int,
        raw_size: int,
    ) -> ProductImage | None:
        """Update non-projection metadata under Product/image serialization."""

        from apps.products.feed_writers import (
            StaleProductFeedWrite,
            locked_product_images_feed_write,
        )

        for _attempt in range(3):
            current = ProductImage.objects.filter(pk=image_id).only(
                'pk', 'product_id',
            ).first()
            if current is None:
                return None
            try:
                with locked_product_images_feed_write(
                    current.product_id,
                    bump=False,
                ) as (_product, images):
                    locked = images.get(image_id)
                    if locked is None or locked.sha256 != expected_sha:
                        raise StaleProductFeedWrite(
                            f'Product image {image_id} changed identity.',
                        )
                    return _update_existing_image_metadata(
                        locked,
                        source_url=source_url,
                        source_id=source_id,
                        phash=phash,
                        actual_width=actual_width,
                        actual_height=actual_height,
                        raw_size=raw_size,
                    )
            except StaleProductFeedWrite:
                continue
        raise StaleProductFeedWrite(
            f'Product image {image_id} changed repeatedly during metadata update.',
        )
