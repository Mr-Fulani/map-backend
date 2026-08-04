"""Business services for provider-neutral media jobs and immutable variants."""

import hashlib
import io
from pathlib import PurePosixPath
from urllib.parse import urlparse

import requests
from django.core.exceptions import ObjectDoesNotExist
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils.timezone import now

from apps.core.url_security import is_safe_public_http_url
from apps.media_processing.models import (
    MediaProcessingJob,
    MediaProcessingPreset,
    MediaProviderPolicy,
    ProductImageVariant,
    TenantMediaSettings,
)
from apps.media_processing.providers.base import (
    MediaOperation,
    MediaProviderRequest,
    MediaProviderResult,
    MediaProviderResultStatus,
)
from apps.media_processing.providers.registry import (
    MediaProviderUnavailable,
    get_media_provider,
    list_media_providers,
)
from apps.media_processing.prompts import build_product_media_prompt


GENERATIVE_OPERATIONS = {
    MediaOperation.REPLACE_BACKGROUND,
    MediaOperation.GENERATIVE_FILL,
}
MAX_PROVIDER_OUTPUT_BYTES = 25 * 1024 * 1024


def _tenant_plan_slug(tenant) -> str:
    try:
        return tenant.subscription.plan.slug
    except (AttributeError, ObjectDoesNotExist):
        return ''


def provider_preferences_for_tenant(
    tenant,
    operations: tuple[MediaOperation, ...],
    preset: MediaProcessingPreset | None = None,
) -> list[str]:
    """Return provider order without embedding tariff names in application code."""
    result: list[str] = []
    if preset:
        result.extend(preset.provider_preferences or [])

    tenant_settings = TenantMediaSettings.objects.filter(tenant=tenant).first()
    preferences = tenant_settings.provider_preferences if tenant_settings else {}
    for operation in operations:
        result.extend(preferences.get(operation.value, []))
    result.extend(preferences.get('*', []))

    plan_slug = _tenant_plan_slug(tenant)
    policies = MediaProviderPolicy.objects.filter(is_active=True).order_by('priority', 'pk')
    for policy in policies:
        if policy.allowed_plan_slugs and plan_slug not in policy.allowed_plan_slugs:
            continue
        if policy.capabilities and not {
            operation.value for operation in operations
        }.issubset(set(policy.capabilities)):
            continue
        result.append(policy.provider_id)

    result.extend(provider.provider_id for provider in list_media_providers())
    return list(dict.fromkeys(provider_id for provider_id in result if provider_id))


def resolve_provider_for_job(job: MediaProcessingJob):
    operations = tuple(MediaOperation(operation) for operation in job.operations)
    if job.provider_id:
        provider = get_media_provider(job.provider_id)
        if not provider.is_configured() or not provider.supports(set(operations)):
            raise MediaProviderUnavailable(
                f'Provider {job.provider_id} is unavailable for requested operations',
            )
        return provider

    for provider_id in provider_preferences_for_tenant(job.tenant, operations, job.preset):
        try:
            provider = get_media_provider(provider_id)
        except LookupError:
            continue
        if provider.is_configured() and provider.supports(set(operations)):
            return provider
    raise MediaProviderUnavailable('Нет доступного провайдера для выбранных операций.')


def create_processing_job(
    *,
    product_image,
    operations: list[str] | None = None,
    parameters: dict | None = None,
    preset: MediaProcessingPreset | None = None,
    provider_id: str = '',
    requested_by=None,
    idempotency_key: str = '',
) -> MediaProcessingJob:
    tenant = product_image.product.tenant
    if preset and preset.tenant_id not in (None, tenant.pk):
        raise ValueError('Пресет принадлежит другому тенанту.')

    effective_operations = operations or (preset.operations if preset else [])
    normalized_operations = [MediaOperation(operation).value for operation in effective_operations]
    if not normalized_operations:
        raise ValueError('Не выбраны операции обработки.')

    tenant_settings = TenantMediaSettings.objects.filter(tenant=tenant).first()
    if (
        not (tenant_settings and tenant_settings.allow_generative_operations)
        and set(map(MediaOperation, normalized_operations)) & GENERATIVE_OPERATIONS
    ):
        raise ValueError('Генеративные операции отключены в настройках тенанта.')

    effective_parameters = {**(preset.parameters if preset else {}), **(parameters or {})}
    if set(map(MediaOperation, normalized_operations)) & GENERATIVE_OPERATIONS:
        effective_parameters.pop('generation_prompt', None)
        effective_parameters.pop('negative_prompt', None)
        effective_parameters.update(build_product_media_prompt(
            product_image.product,
            str(effective_parameters.get('background_style') or 'white_studio'),
        ))
    defaults = {
        'product_image': product_image,
        'preset': preset,
        'operations': normalized_operations,
        'parameters': effective_parameters,
        'provider_id': provider_id,
        'requested_by': requested_by,
    }
    if idempotency_key:
        job, _ = MediaProcessingJob.objects.get_or_create(
            tenant=tenant,
            idempotency_key=idempotency_key,
            defaults=defaults,
        )
        return job
    return MediaProcessingJob.objects.create(tenant=tenant, **defaults)


def submit_job(job: MediaProcessingJob, callback_url: str = '') -> MediaProcessingJob:
    provider = resolve_provider_for_job(job)
    operations = tuple(MediaOperation(operation) for operation in job.operations)
    job.provider_id = provider.provider_id
    estimate = provider.estimate_cost(operations, job.parameters)
    if estimate is not None:
        job.estimated_credits = estimate
    job.status = MediaProcessingJob.Status.PROCESSING
    job.started_at = job.started_at or now()
    job.error_code = ''
    job.error_message = ''
    job.save(update_fields=[
        'provider_id', 'estimated_credits', 'status', 'started_at',
        'error_code', 'error_message', 'updated_at',
    ])

    input_url = default_storage.url(job.product_image.s3_key)
    result = provider.process(MediaProviderRequest(
        input_url=input_url,
        operations=operations,
        parameters=job.parameters,
        callback_url=callback_url,
        idempotency_key=job.idempotency_key,
    ))
    return apply_provider_result(job, result)


def apply_provider_result(
    job: MediaProcessingJob,
    result: MediaProviderResult,
) -> MediaProcessingJob:
    job.provider_job_id = result.provider_job_id or job.provider_job_id
    job.provider_metadata = result.metadata
    if result.actual_cost is not None:
        job.charged_credits = result.actual_cost

    if result.status == MediaProviderResultStatus.PENDING:
        job.status = MediaProcessingJob.Status.SUBMITTED
        job.save(update_fields=[
            'provider_job_id', 'provider_metadata', 'charged_credits',
            'status', 'updated_at',
        ])
        return job

    if result.status == MediaProviderResultStatus.FAILED:
        return fail_job(job, result.error_code or 'provider_error', result.error_message)

    try:
        raw_bytes, content_type = _provider_result_bytes(result)
        variant = _store_variant(job, raw_bytes, content_type)
    except Exception as exc:
        return fail_job(job, 'invalid_provider_output', str(exc))

    job.status = MediaProcessingJob.Status.SUCCEEDED
    job.finished_at = now()
    job.provider_metadata = {**job.provider_metadata, 'variant_id': variant.pk}
    job.save(update_fields=[
        'provider_job_id', 'provider_metadata', 'charged_credits',
        'status', 'finished_at', 'updated_at',
    ])
    return job


def fail_job(job: MediaProcessingJob, error_code: str, error_message: str) -> MediaProcessingJob:
    job.status = MediaProcessingJob.Status.FAILED
    job.error_code = error_code[:100]
    job.error_message = error_message
    job.finished_at = now()
    job.save(update_fields=[
        'status', 'error_code', 'error_message', 'finished_at', 'updated_at',
    ])
    return job


def activate_variant(variant: ProductImageVariant) -> ProductImageVariant:
    """Select a derived file without overwriting or deleting the original."""
    with transaction.atomic():
        ProductImageVariant.objects.select_for_update().filter(
            product_image=variant.product_image,
            is_active=True,
        ).exclude(pk=variant.pk).update(is_active=False)
        variant.is_active = True
        variant.save(update_fields=['is_active', 'updated_at'])
    return variant


def delivery_s3_key(product_image) -> str:
    """Use the approved variant when present and preserve current behavior otherwise."""
    prefetched = getattr(product_image, '_prefetched_objects_cache', {}).get('variants')
    if prefetched is not None:
        active = next((variant for variant in prefetched if variant.is_active), None)
    else:
        active = product_image.variants.filter(is_active=True).first()
    return active.s3_key if active else product_image.s3_key


def _provider_result_bytes(result: MediaProviderResult) -> tuple[bytes, str]:
    if result.output_bytes is not None:
        raw_bytes = result.output_bytes
        content_type = result.output_content_type or 'image/jpeg'
    elif result.output_url:
        if not is_safe_public_http_url(result.output_url):
            raise ValueError('Провайдер вернул небезопасный URL результата.')
        response = requests.get(result.output_url, timeout=30, stream=True)
        response.raise_for_status()
        content_type = response.headers.get('Content-Type', '').split(';', 1)[0]
        chunks = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_PROVIDER_OUTPUT_BYTES:
                raise ValueError('Некорректный размер результата провайдера.')
            chunks.append(chunk)
        raw_bytes = b''.join(chunks)
    else:
        raise ValueError('Провайдер не вернул изображение.')

    if not raw_bytes or len(raw_bytes) > MAX_PROVIDER_OUTPUT_BYTES:
        raise ValueError('Некорректный размер результата провайдера.')
    if content_type and not content_type.startswith('image/'):
        raise ValueError('Провайдер вернул не изображение.')
    return raw_bytes, content_type or 'image/jpeg'


def _store_variant(
    job: MediaProcessingJob,
    raw_bytes: bytes,
    content_type: str,
) -> ProductImageVariant:
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(raw_bytes))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError('Результат провайдера не удалось открыть как изображение.') from exc

    sha = hashlib.sha256(raw_bytes).hexdigest()
    existing = ProductImageVariant.objects.filter(
        product_image=job.product_image,
        sha256=sha,
    ).first()
    if existing:
        return existing

    extension = _extension_for_content_type(content_type, result_url='')
    original_path = PurePosixPath(job.product_image.s3_key)
    variant_key = str(
        original_path.with_name(
            f'{original_path.stem}_variant_{job.pk}_{sha[:12]}{extension}',
        ),
    )
    saved_key = default_storage.save(variant_key, io.BytesIO(raw_bytes))
    return ProductImageVariant.objects.create(
        tenant=job.tenant,
        product_image=job.product_image,
        job=job,
        provider_id=job.provider_id,
        operations=job.operations,
        parameters=job.parameters,
        s3_key=saved_key,
        content_type=content_type,
        width=image.width,
        height=image.height,
        file_size_kb=max(1, len(raw_bytes) // 1024),
        sha256=sha,
    )


def _extension_for_content_type(content_type: str, result_url: str = '') -> str:
    mapping = {
        'image/png': '.png',
        'image/webp': '.webp',
        'image/jpeg': '.jpg',
        'image/jpg': '.jpg',
    }
    if content_type in mapping:
        return mapping[content_type]
    suffix = PurePosixPath(urlparse(result_url).path).suffix.lower()
    return suffix if suffix in {'.jpg', '.jpeg', '.png', '.webp'} else '.jpg'
