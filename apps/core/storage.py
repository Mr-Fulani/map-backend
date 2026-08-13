import logging
from collections.abc import Iterable
from typing import Any

from django.core.files.storage import default_storage
from django.db import DEFAULT_DB_ALIAS, transaction
from django.db.models import Q


logger = logging.getLogger(__name__)


def delete_storage_keys(
    keys: Iterable[str],
    *,
    storage: Any = None,
) -> None:
    """Best-effort deletion for keys that are no longer referenced by the DB."""
    backend = default_storage if storage is None else storage
    for key in dict.fromkeys(str(key).strip() for key in keys if key):
        try:
            # Storage.delete() is expected to be idempotent. Avoid a separate
            # exists()/HEAD request because it adds a race and doubles S3 I/O.
            backend.delete(key)
        except Exception:
            logger.warning('Storage delete failed: %s', key, exc_info=True)


def storage_key_is_referenced(
    key: str,
    *,
    using: str = DEFAULT_DB_ALIAS,
) -> bool:
    """Return whether any persisted media record still references ``key``.

    Storage keys were not historically unique, including across media tables.
    Keep this list centralized so every post-commit deletion applies the same
    reference-safety rule.
    """
    # Import lazily: core.storage is imported while product/media app configs
    # register their signals, before importing these models here would be safe.
    from apps.media_processing.models import ProductImageVariant
    from apps.products.models import ProductImage, TenantCatalogCategory

    return (
        ProductImage.objects.using(using).filter(
            Q(s3_key=key) | Q(s3_key_preview=key) | Q(s3_key_thumb=key),
        ).exists()
        or ProductImageVariant.objects.using(using).filter(s3_key=key).exists()
        or TenantCatalogCategory.objects.using(using).filter(
            default_image_s3_key=key,
        ).exists()
    )


def delete_unreferenced_storage_keys(
    keys: Iterable[str],
    *,
    storage: Any = None,
    using: str = DEFAULT_DB_ALIAS,
) -> None:
    """Best-effort delete for keys proven unreferenced after a DB commit.

    Reference-check failures deliberately keep the object. A leaked object can
    be reconciled later; deleting a still-referenced object is irreversible.
    """
    backend = default_storage if storage is None else storage
    normalized_keys = dict.fromkeys(str(key).strip() for key in keys if key)
    for key in normalized_keys:
        try:
            if storage_key_is_referenced(key, using=using):
                continue
        except Exception:
            logger.warning(
                'Storage reference check failed; keeping object: %s',
                key,
                exc_info=True,
            )
            continue

        try:
            # Storage.delete() is idempotent. Do not add a separate HEAD call:
            # it would introduce another race and double object-storage I/O.
            backend.delete(key)
        except Exception:
            logger.warning('Storage delete failed: %s', key, exc_info=True)


def delete_storage_keys_on_commit(
    keys: Iterable[str],
    *,
    storage: Any = None,
    using: str = DEFAULT_DB_ALIAS,
) -> None:
    """After commit, delete only keys no longer referenced by any media row."""
    normalized_keys = tuple(dict.fromkeys(str(key).strip() for key in keys if key))
    if not normalized_keys:
        return
    transaction.on_commit(
        lambda: delete_unreferenced_storage_keys(
            normalized_keys,
            storage=storage,
            using=using,
        ),
        using=using,
        robust=True,
    )
