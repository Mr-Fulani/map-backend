"""Business services for provider-neutral media jobs and immutable variants."""

import hashlib
import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from urllib.parse import urlparse

import requests
from django.core.cache import caches
from django.core.exceptions import ObjectDoesNotExist
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils.timezone import now

from apps.billing.ai_wallet import (
    AIReservation,
    AIWalletService,
    InsufficientAICredits,
)
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


class MediaProviderRateLimitExceeded(MediaProviderUnavailable):
    """The configured provider quota has been exhausted for the current minute."""


@dataclass(frozen=True)
class ResolvedMediaProvider:
    provider: object
    policy: MediaProviderPolicy
    estimated_credits: Decimal


def _tenant_plan_slug(tenant) -> str:
    try:
        subscription = tenant.subscription
    except (AttributeError, ObjectDoesNotExist):
        return ''
    return subscription.plan.slug if subscription.is_active else ''


def _policy_credit_cost(
    policy: MediaProviderPolicy,
    operations: tuple[MediaOperation, ...],
) -> Decimal:
    """Return the tenant tariff cost; even free operations require an explicit zero."""
    total = Decimal('0')
    for operation in operations:
        costs = policy.operation_credit_costs or {}
        if operation.value not in costs:
            raise MediaProviderUnavailable(
                f'Provider {policy.provider_id} has no credit cost for {operation.value}',
            )
        raw_cost = costs[operation.value]
        try:
            cost = Decimal(str(raw_cost))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise MediaProviderUnavailable(
                f'Provider {policy.provider_id} has an invalid credit policy',
            ) from exc
        if not cost.is_finite() or cost < 0:
            raise MediaProviderUnavailable(
                f'Provider {policy.provider_id} has an invalid credit policy',
            )
        total += cost
    return total


def _policy_denial_reason(
    policy: MediaProviderPolicy | None,
    tenant,
    operations: tuple[MediaOperation, ...],
) -> str:
    if policy is None:
        return 'provider has no allow policy'
    if not policy.is_active:
        return 'provider policy is disabled'
    plan_slug = _tenant_plan_slug(tenant)
    if not plan_slug:
        return 'tenant has no active subscription plan'
    if policy.allowed_plan_slugs and plan_slug not in policy.allowed_plan_slugs:
        return 'provider is unavailable for the tenant plan'
    required = {operation.value for operation in operations}
    if not required.issubset(set(policy.capabilities or [])):
        return 'provider policy does not allow the requested operations'
    return ''


def _resolve_provider_candidate(
    tenant,
    operations: tuple[MediaOperation, ...],
    provider_id: str,
    policies: dict[str, MediaProviderPolicy],
) -> ResolvedMediaProvider:
    normalized_id = (provider_id or '').strip().lower()
    policy = policies.get(normalized_id)
    denial_reason = _policy_denial_reason(policy, tenant, operations)
    if denial_reason:
        raise MediaProviderUnavailable(f'Provider {normalized_id}: {denial_reason}')
    try:
        provider = get_media_provider(normalized_id)
    except LookupError as exc:
        raise MediaProviderUnavailable(f'Unknown media provider: {normalized_id}') from exc
    if not provider.is_configured() or not provider.supports(set(operations)):
        raise MediaProviderUnavailable(
            f'Provider {normalized_id} is unavailable for requested operations',
        )
    return ResolvedMediaProvider(
        provider=provider,
        policy=policy,
        estimated_credits=_policy_credit_cost(policy, operations),
    )


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

    result.extend(
        MediaProviderPolicy.objects.order_by('priority', 'pk').values_list(
            'provider_id', flat=True,
        ),
    )

    result.extend(provider.provider_id for provider in list_media_providers())
    return list(dict.fromkeys(provider_id for provider_id in result if provider_id))


def resolve_provider_for_request(
    tenant,
    operations: tuple[MediaOperation, ...],
    *,
    preset: MediaProcessingPreset | None = None,
    provider_id: str = '',
) -> ResolvedMediaProvider:
    """Resolve one provider through the same fail-closed tenant allow policy."""
    policies = {
        policy.provider_id: policy
        for policy in MediaProviderPolicy.objects.all()
    }
    if provider_id:
        return _resolve_provider_candidate(
            tenant, operations, provider_id, policies,
        )

    for candidate_id in provider_preferences_for_tenant(tenant, operations, preset):
        try:
            return _resolve_provider_candidate(
                tenant, operations, candidate_id, policies,
            )
        except MediaProviderUnavailable:
            continue
    raise MediaProviderUnavailable('Нет доступного провайдера для выбранных операций.')


def resolve_provider_for_job(job: MediaProcessingJob):
    operations = tuple(MediaOperation(operation) for operation in job.operations)
    return resolve_provider_for_request(
        job.tenant,
        operations,
        preset=job.preset,
        provider_id=job.provider_id,
    ).provider


def _preflight_provider_request(
    tenant,
    operations: tuple[MediaOperation, ...],
    *,
    preset: MediaProcessingPreset | None,
    provider_id: str,
) -> None:
    try:
        resolution = resolve_provider_for_request(
            tenant,
            operations,
            preset=preset,
            provider_id=provider_id,
        )
    except MediaProviderUnavailable as exc:
        raise ValueError(str(exc)) from exc
    if (
        resolution.estimated_credits > 0
        and AIWalletService.summary(tenant)['available'] < resolution.estimated_credits
    ):
        raise ValueError(
            'Недостаточно AI-кредитов для выбранного медиа-провайдера.',
        )


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
    if len(normalized_operations) != len(set(normalized_operations)):
        raise ValueError('Операции обработки не должны повторяться.')

    tenant_settings = TenantMediaSettings.objects.filter(tenant=tenant).first()
    if (
        not (tenant_settings and tenant_settings.allow_generative_operations)
        and set(map(MediaOperation, normalized_operations)) & GENERATIVE_OPERATIONS
    ):
        raise ValueError('Генеративные операции отключены в настройках тенанта.')

    normalized_operation_values = tuple(map(MediaOperation, normalized_operations))
    _preflight_provider_request(
        tenant,
        normalized_operation_values,
        preset=preset,
        provider_id=provider_id,
    )

    effective_parameters = {**(preset.parameters if preset else {}), **(parameters or {})}
    if set(normalized_operation_values) & GENERATIVE_OPERATIONS:
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
        job, created = MediaProcessingJob.objects.get_or_create(
            tenant=tenant,
            idempotency_key=idempotency_key,
            defaults=defaults,
        )
        job._created_for_request = created
        return job
    job = MediaProcessingJob.objects.create(tenant=tenant, **defaults)
    job._created_for_request = True
    return job


def _consume_provider_rate_limit(policy: MediaProviderPolicy) -> None:
    limit = policy.requests_per_minute
    if not limit:
        return
    minute = now().strftime('%Y%m%d%H%M')
    key = f'media-provider-rate:{policy.provider_id}:{minute}'
    coordination_cache = caches['coordination']
    coordination_cache.add(key, 0, timeout=120)
    current = coordination_cache.incr(key)
    if current > limit:
        raise MediaProviderRateLimitExceeded(
            f'Provider {policy.provider_id} rate limit exceeded',
        )


def _credit_state(job: MediaProcessingJob) -> dict:
    metadata = job.provider_metadata if isinstance(job.provider_metadata, dict) else {}
    state = metadata.get('credit_reservation', {})
    return state if isinstance(state, dict) else {}


def _reservation_from_state(state: dict) -> AIReservation | None:
    key = state.get('key')
    amount = state.get('amount')
    if not key or amount is None:
        return None
    try:
        return AIReservation(str(key), Decimal(str(amount)))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _reserve_job_credits(
    job: MediaProcessingJob,
    amount: Decimal,
) -> AIReservation | None:
    if amount <= 0:
        return None
    with transaction.atomic():
        locked = MediaProcessingJob.objects.select_for_update().get(pk=job.pk)
        state = _credit_state(locked)
        existing = _reservation_from_state(state)
        if existing and state.get('status') in {'reserved', 'settled'}:
            job.provider_metadata = locked.provider_metadata
            job.charged_credits = locked.charged_credits
            return existing

        attempt = int(state.get('attempt') or 0) + 1
        key = f'media-job:{job.pk}:credits:{attempt}'
        try:
            reservation = AIWalletService.reserve(
                locked.tenant,
                amount,
                key=key,
                details={
                    'kind': 'media_processing',
                    'job_id': job.pk,
                    'provider_id': job.provider_id,
                },
            )
        except InsufficientAICredits as exc:
            raise MediaProviderUnavailable(str(exc)) from exc
        metadata = dict(locked.provider_metadata or {})
        metadata['credit_reservation'] = {
            'key': reservation.key,
            'amount': str(reservation.amount),
            'attempt': attempt,
            'status': 'reserved',
        }
        locked.provider_metadata = metadata
        locked.save(update_fields=['provider_metadata', 'updated_at'])
        job.provider_metadata = metadata
        return reservation


def _release_job_credits(job: MediaProcessingJob, *, reason: str) -> None:
    with transaction.atomic():
        locked = MediaProcessingJob.objects.select_for_update().get(pk=job.pk)
        state = _credit_state(locked)
        reservation = _reservation_from_state(state)
        if not reservation or state.get('status') != 'reserved':
            job.provider_metadata = locked.provider_metadata
            return
        AIWalletService.release(locked.tenant, reservation, reason=reason)
        metadata = {
            **(locked.provider_metadata or {}),
            **(job.provider_metadata or {}),
        }
        metadata['credit_reservation'] = {**state, 'status': 'released'}
        locked.provider_metadata = metadata
        locked.save(update_fields=['provider_metadata', 'updated_at'])
        job.provider_metadata = metadata


def _settle_job_credits(job: MediaProcessingJob) -> None:
    with transaction.atomic():
        locked = MediaProcessingJob.objects.select_for_update().get(pk=job.pk)
        state = _credit_state(locked)
        reservation = _reservation_from_state(state)
        metadata = {
            **(locked.provider_metadata or {}),
            **(job.provider_metadata or {}),
        }
        if not reservation:
            locked.charged_credits = Decimal('0')
            locked.provider_metadata = metadata
        elif state.get('status') == 'settled':
            locked.provider_metadata = metadata
            locked.save(update_fields=['provider_metadata', 'updated_at'])
            job.provider_metadata = metadata
            job.charged_credits = locked.charged_credits
            return
        elif state.get('status') != 'reserved':
            return
        else:
            locked.charged_credits = AIWalletService.settle(
                locked.tenant,
                reservation,
                reservation.amount,
                details={
                    'kind': 'media_processing',
                    'job_id': job.pk,
                    'provider_id': job.provider_id,
                },
            )
            metadata['credit_reservation'] = {**state, 'status': 'settled'}
            locked.provider_metadata = metadata
        locked.save(update_fields=[
            'charged_credits', 'provider_metadata', 'updated_at',
        ])
        job.provider_metadata = locked.provider_metadata
        job.charged_credits = locked.charged_credits


def submit_job(job: MediaProcessingJob, callback_url: str = '') -> MediaProcessingJob:
    operations = tuple(MediaOperation(operation) for operation in job.operations)
    resolution = resolve_provider_for_request(
        job.tenant,
        operations,
        preset=job.preset,
        provider_id=job.provider_id,
    )
    provider = resolution.provider
    job.provider_id = provider.provider_id
    job.estimated_credits = resolution.estimated_credits
    _reserve_job_credits(job, resolution.estimated_credits)
    try:
        _consume_provider_rate_limit(resolution.policy)
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
    except Exception:
        _release_job_credits(job, reason='provider_submission_failed')
        raise
    return apply_provider_result(job, result)


def apply_provider_result(
    job: MediaProcessingJob,
    result: MediaProviderResult,
) -> MediaProcessingJob:
    job.provider_job_id = result.provider_job_id or job.provider_job_id
    job.provider_metadata = {**(job.provider_metadata or {}), **result.metadata}
    if result.actual_cost is not None:
        job.provider_metadata['provider_actual_cost'] = str(result.actual_cost)

    if result.status == MediaProviderResultStatus.PENDING:
        _settle_job_credits(job)
        job.status = MediaProcessingJob.Status.SUBMITTED
        job.save(update_fields=[
            'provider_job_id', 'provider_metadata', 'charged_credits',
            'status', 'updated_at',
        ])
        return job

    if result.status == MediaProviderResultStatus.FAILED:
        _release_job_credits(job, reason=result.error_code or 'provider_error')
        return fail_job(job, result.error_code or 'provider_error', result.error_message)

    try:
        raw_bytes, content_type = _provider_result_bytes(result)
        variant = _store_variant(job, raw_bytes, content_type)
    except Exception as exc:
        _release_job_credits(job, reason='invalid_provider_output')
        return fail_job(job, 'invalid_provider_output', str(exc))

    _settle_job_credits(job)
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
