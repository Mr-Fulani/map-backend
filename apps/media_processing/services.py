"""Business services for provider-neutral media jobs and immutable variants."""

import base64
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Protocol, cast
from urllib.parse import urlparse
import uuid

from django.conf import settings
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
from apps.core.image_security import validate_image_pixel_budget
from apps.core.idempotency import (
    canonical_payload_fingerprint,
    raise_on_fingerprint_conflict,
)
from apps.core.storage import delete_storage_keys
from apps.core.url_security import (
    REDIRECT_SAME_ORIGIN,
    request_public_http_url,
)
from apps.datasources.encryption import decrypt, encrypt
from apps.media_processing.models import (
    MediaProcessingJob,
    MediaProcessingPreset,
    MediaProviderPolicy,
    ProductImageVariant,
    TenantMediaSettings,
)
from apps.media_processing.providers.base import (
    BaseMediaProvider,
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
from apps.products.models import ProductImage


GENERATIVE_OPERATIONS = {
    MediaOperation.REPLACE_BACKGROUND,
    MediaOperation.GENERATIVE_FILL,
}
_URL_IN_ERROR_RE = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)
_GENERIC_PROVIDER_FAILURE = 'Медиа-провайдер не смог обработать изображение.'
_GENERIC_PROVIDER_OUTPUT_FAILURE = (
    'Результат медиа-провайдера не прошёл проверку безопасности.'
)


class MediaProviderRateLimitExceeded(MediaProviderUnavailable):
    """The configured provider quota has been exhausted for the current minute."""


class MediaProviderOutcomeUncertain(RuntimeError):
    """The provider may have accepted an operation before transport failed."""


class MediaProviderCheckpointNotApplicable(RuntimeError):
    """A known response exists, but its bounded checkpoint cannot be applied."""


class MediaProviderCheckpointApplyInProgress(RuntimeError):
    """Another worker owns a fresh durable local-apply claim."""


class _CreationTrackedJob(Protocol):
    _created_for_request: bool


@dataclass(frozen=True)
class ResolvedMediaProvider:
    provider: BaseMediaProvider
    policy: MediaProviderPolicy
    estimated_credits: Decimal


_PROVIDER_RESPONSE_CHECKPOINT_VERSION = 1
_PROVIDER_RESPONSE_BINARY_MAX_BYTES = 2 * 1024 * 1024
_PROVIDER_RESPONSE_METADATA_MAX_BYTES = 64 * 1024
_PROVIDER_RESPONSE_URL_MAX_CHARS = 8192
_PROVIDER_RESPONSE_ID_MAX_CHARS = 4096
_PROVIDER_RESPONSE_CHECKPOINT_MAX_BYTES = 3 * 1024 * 1024
_PROVIDER_RESPONSE_APPLY_LEASE = timedelta(minutes=10)


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
    if policy is None:
        # Kept explicit for static narrowing; the fail-closed denial above is
        # the only reachable branch for a missing policy.
        raise MediaProviderUnavailable(f'Provider {normalized_id}: provider has no allow policy')
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


def media_processing_request_fingerprint(
    *,
    product_image_id: int,
    preset_id: int | None,
    operations: list[str] | None,
    parameters: dict | None,
    provider_id: str,
) -> str:
    """Fingerprint only the stable, caller-supplied media-processing intent."""
    requested_operations = [
        MediaOperation(operation).value for operation in (operations or [])
    ]
    return canonical_payload_fingerprint({
        'operations': requested_operations,
        'parameters': parameters or {},
        'preset_id': preset_id,
        'product_image_id': product_image_id,
        'provider_id': provider_id,
    })


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

    request_fingerprint = media_processing_request_fingerprint(
        product_image_id=product_image.pk,
        preset_id=preset.pk if preset else None,
        operations=operations,
        parameters=parameters,
        provider_id=provider_id,
    )
    if idempotency_key:
        existing_job = MediaProcessingJob.objects.filter(
            tenant=tenant,
            idempotency_key=str(idempotency_key),
        ).first()
        if existing_job is not None:
            raise_on_fingerprint_conflict(
                existing_job.request_fingerprint,
                request_fingerprint,
            )
            cast(_CreationTrackedJob, existing_job)._created_for_request = False
            return existing_job

    effective_operations = operations or (preset.operations if preset else [])
    normalized_operations = [MediaOperation(operation).value for operation in effective_operations]
    if not normalized_operations:
        raise ValueError('Не выбраны операции обработки.')
    if len(normalized_operations) != len(set(normalized_operations)):
        raise ValueError('Операции обработки не должны повторяться.')

    normalized_operation_values = tuple(map(MediaOperation, normalized_operations))

    def build_defaults() -> dict:
        tenant_settings = TenantMediaSettings.objects.filter(tenant=tenant).first()
        if (
            not (tenant_settings and tenant_settings.allow_generative_operations)
            and set(normalized_operation_values) & GENERATIVE_OPERATIONS
        ):
            raise ValueError('Генеративные операции отключены в настройках тенанта.')
        effective_parameters = {
            **(preset.parameters if preset else {}),
            **(parameters or {}),
        }
        if set(normalized_operation_values) & GENERATIVE_OPERATIONS:
            effective_parameters.pop('generation_prompt', None)
            effective_parameters.pop('negative_prompt', None)
            effective_parameters.update(build_product_media_prompt(
                product_image.product,
                str(effective_parameters.get('background_style') or 'white_studio'),
            ))
        return {
            'product_image': product_image,
            'preset': preset,
            'operations': normalized_operations,
            'parameters': effective_parameters,
            'provider_id': provider_id,
            'requested_by': requested_by,
            'request_fingerprint': request_fingerprint,
        }
    if idempotency_key:
        with transaction.atomic():
            type(tenant).objects.select_for_update().only('pk').get(pk=tenant.pk)
            existing_job = MediaProcessingJob.objects.filter(
                tenant=tenant,
                idempotency_key=str(idempotency_key),
            ).first()
            if existing_job is not None:
                raise_on_fingerprint_conflict(
                    existing_job.request_fingerprint,
                    request_fingerprint,
                )
                cast(_CreationTrackedJob, existing_job)._created_for_request = False
                return existing_job
            defaults = build_defaults()
            _preflight_provider_request(
                tenant,
                normalized_operation_values,
                preset=preset,
                provider_id=provider_id,
            )
            job, created = MediaProcessingJob.objects.get_or_create(
                tenant=tenant,
                idempotency_key=str(idempotency_key),
                defaults=defaults,
            )
            if not created:
                raise_on_fingerprint_conflict(
                    job.request_fingerprint,
                    request_fingerprint,
                )
                cast(_CreationTrackedJob, job)._created_for_request = False
                return job
            cast(_CreationTrackedJob, job)._created_for_request = True
            return job

    defaults = build_defaults()
    _preflight_provider_request(
        tenant,
        normalized_operation_values,
        preset=preset,
        provider_id=provider_id,
    )
    job = MediaProcessingJob.objects.create(tenant=tenant, **defaults)
    cast(_CreationTrackedJob, job)._created_for_request = True
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


def _canonical_provider_checkpoint(payload: dict) -> bytes:
    try:
        return json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(',', ':'),
            sort_keys=True,
        ).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise MediaProviderCheckpointNotApplicable(
            'Ответ провайдера нельзя безопасно сериализовать.',
        ) from exc


def _bounded_checkpoint_metadata(metadata: object) -> tuple[dict, bool]:
    if not isinstance(metadata, dict):
        return {}, bool(metadata)
    try:
        raw = _canonical_provider_checkpoint(metadata)
    except MediaProviderCheckpointNotApplicable:
        return {}, True
    if len(raw) > _PROVIDER_RESPONSE_METADATA_MAX_BYTES:
        return {}, True
    normalized = json.loads(raw.decode('utf-8'))
    return normalized if isinstance(normalized, dict) else {}, False


def _bounded_checkpoint_text(value: object, max_chars: int) -> tuple[str, bool]:
    normalized = str(value or '')
    if len(normalized) > max_chars:
        return '', True
    return normalized, False


def _serialize_provider_result_checkpoint(
    result: MediaProviderResult,
) -> tuple[dict, bytes, str]:
    try:
        status = MediaProviderResultStatus(result.status).value
    except (TypeError, ValueError):
        status = 'invalid'

    provider_job_id, provider_job_id_omitted = _bounded_checkpoint_text(
        result.provider_job_id,
        _PROVIDER_RESPONSE_ID_MAX_CHARS,
    )
    output_url, output_url_omitted = _bounded_checkpoint_text(
        result.output_url,
        _PROVIDER_RESPONSE_URL_MAX_CHARS,
    )
    output_content_type, content_type_omitted = _bounded_checkpoint_text(
        result.output_content_type,
        255,
    )
    metadata, metadata_omitted = _bounded_checkpoint_metadata(result.metadata)

    output_bytes = result.output_bytes
    output_bytes_present = output_bytes is not None
    output_bytes_omitted = False
    output_bytes_b64 = ''
    output_bytes_size = 0
    output_bytes_sha256 = ''
    if output_bytes_present:
        if not isinstance(output_bytes, bytes):
            output_bytes_omitted = True
        else:
            output_bytes_size = len(output_bytes)
            output_bytes_sha256 = hashlib.sha256(output_bytes).hexdigest()
            if output_bytes_size > min(
                settings.MEDIA_PROVIDER_OUTPUT_MAX_BYTES,
                _PROVIDER_RESPONSE_BINARY_MAX_BYTES,
            ):
                output_bytes_omitted = True
            else:
                output_bytes_b64 = base64.b64encode(output_bytes).decode('ascii')

    actual_cost = ''
    if result.actual_cost is not None:
        try:
            normalized_cost = Decimal(result.actual_cost)
        except (InvalidOperation, TypeError, ValueError):
            normalized_cost = Decimal('NaN')
        if normalized_cost.is_finite() and normalized_cost >= 0:
            actual_cost = str(normalized_cost)

    payload = {
        'version': _PROVIDER_RESPONSE_CHECKPOINT_VERSION,
        'status': status,
        'provider_job_id': provider_job_id,
        'provider_job_id_omitted': provider_job_id_omitted,
        # URLs may contain provider bearer tokens. The complete payload is
        # encrypted before persistence and is never exposed via serializers.
        'output_url': output_url,
        'output_url_omitted': output_url_omitted,
        'output_bytes_b64': output_bytes_b64,
        'output_bytes_present': output_bytes_present,
        'output_bytes_omitted': output_bytes_omitted,
        'output_bytes_size': output_bytes_size,
        'output_bytes_sha256': output_bytes_sha256,
        'output_content_type': output_content_type,
        'content_type_omitted': content_type_omitted,
        'metadata': metadata,
        'metadata_omitted': metadata_omitted,
        'actual_cost': actual_cost,
        'error_code': str(result.error_code or '')[:100],
    }
    canonical = _canonical_provider_checkpoint(payload)
    if len(canonical) > _PROVIDER_RESPONSE_CHECKPOINT_MAX_BYTES:
        raise MediaProviderCheckpointNotApplicable(
            'Ответ провайдера превышает лимит durable checkpoint.',
        )
    return payload, canonical, hashlib.sha256(canonical).hexdigest()


def _copy_provider_checkpoint_state(
    target: MediaProcessingJob,
    source: MediaProcessingJob,
) -> None:
    for field in (
        'provider_job_id', 'provider_response_enc', 'provider_response_digest',
        'provider_response_status', 'provider_response_state',
        'provider_response_recorded_at', 'provider_response_apply_token',
        'provider_response_apply_claimed_at',
        'provider_response_resolved_at',
    ):
        setattr(target, field, getattr(source, field))


def _checkpoint_provider_result(
    job: MediaProcessingJob,
    result: MediaProviderResult,
) -> None:
    """Persist the exact bounded response before accounting or local I/O."""
    payload, _canonical, digest = _serialize_provider_result_checkpoint(result)
    encrypted = encrypt(payload)
    with transaction.atomic():
        locked = MediaProcessingJob.objects.select_for_update().get(pk=job.pk)
        if locked.provider_response_state:
            if locked.provider_response_digest != digest:
                raise MediaProviderOutcomeUncertain(
                    'Для задачи уже сохранён другой ответ провайдера.',
                )
            _copy_provider_checkpoint_state(job, locked)
            return

        checkpointed_at = now()
        locked.provider_response_enc = encrypted
        locked.provider_response_digest = digest
        locked.provider_response_status = (
            payload['status']
            if payload['status'] in MediaProcessingJob.ProviderResponseStatus.values
            else ''
        )
        locked.provider_response_state = (
            MediaProcessingJob.ProviderResponseState.RECORDED
        )
        locked.provider_response_recorded_at = checkpointed_at
        locked.provider_response_apply_token = None
        locked.provider_response_apply_claimed_at = None
        locked.provider_response_resolved_at = None
        provider_job_id = str(payload.get('provider_job_id') or '')
        if provider_job_id:
            locked.provider_job_id = provider_job_id[:255]
        locked.save(update_fields=[
            'provider_job_id', 'provider_response_enc',
            'provider_response_digest', 'provider_response_status',
            'provider_response_state', 'provider_response_recorded_at',
            'provider_response_apply_token',
            'provider_response_apply_claimed_at',
            'provider_response_resolved_at', 'updated_at',
        ])
        _copy_provider_checkpoint_state(job, locked)


def _provider_result_from_checkpoint(job: MediaProcessingJob) -> MediaProviderResult:
    encrypted = job.provider_response_enc
    if encrypted is None:
        raise MediaProviderCheckpointNotApplicable(
            'Durable checkpoint ответа провайдера отсутствует.',
        )
    try:
        payload = decrypt(bytes(encrypted))
        if not isinstance(payload, dict):
            raise ValueError('checkpoint is not an object')
        canonical = _canonical_provider_checkpoint(payload)
    except Exception as exc:
        raise MediaProviderCheckpointNotApplicable(
            'Durable checkpoint ответа провайдера повреждён.',
        ) from exc
    if (
        payload.get('version') != _PROVIDER_RESPONSE_CHECKPOINT_VERSION
        or hashlib.sha256(canonical).hexdigest() != job.provider_response_digest
    ):
        raise MediaProviderCheckpointNotApplicable(
            'Durable checkpoint ответа провайдера не прошёл проверку целостности.',
        )
    try:
        status = MediaProviderResultStatus(str(payload.get('status') or ''))
    except ValueError as exc:
        raise MediaProviderCheckpointNotApplicable(
            'Checkpoint содержит неизвестный статус провайдера.',
        ) from exc

    output_bytes = None
    if payload.get('output_bytes_present'):
        if payload.get('output_bytes_omitted'):
            raise MediaProviderCheckpointNotApplicable(
                'Бинарный ответ провайдера превышает лимит checkpoint.',
            )
        try:
            output_bytes = base64.b64decode(
                str(payload.get('output_bytes_b64') or ''),
                validate=True,
            )
        except ValueError as exc:
            raise MediaProviderCheckpointNotApplicable(
                'Бинарный ответ в checkpoint повреждён.',
            ) from exc
        if (
            len(output_bytes) != int(payload.get('output_bytes_size') or 0)
            or hashlib.sha256(output_bytes).hexdigest()
            != str(payload.get('output_bytes_sha256') or '')
            or len(output_bytes) > min(
                settings.MEDIA_PROVIDER_OUTPUT_MAX_BYTES,
                _PROVIDER_RESPONSE_BINARY_MAX_BYTES,
            )
        ):
            raise MediaProviderCheckpointNotApplicable(
                'Бинарный ответ в checkpoint не прошёл проверку целостности.',
            )
    if payload.get('output_url_omitted') and output_bytes is None:
        raise MediaProviderCheckpointNotApplicable(
            'URL результата не поместился в checkpoint.',
        )
    if (
        payload.get('provider_job_id_omitted')
        and status == MediaProviderResultStatus.PENDING
    ):
        raise MediaProviderCheckpointNotApplicable(
            'Идентификатор асинхронной задачи не поместился в checkpoint.',
        )
    metadata = payload.get('metadata')
    if not isinstance(metadata, dict):
        metadata = {}
    actual_cost = None
    if payload.get('actual_cost'):
        try:
            actual_cost = Decimal(str(payload['actual_cost']))
        except (InvalidOperation, TypeError, ValueError):
            actual_cost = None
    return MediaProviderResult(
        status=status,
        provider_job_id=str(payload.get('provider_job_id') or ''),
        output_url=str(payload.get('output_url') or ''),
        output_bytes=output_bytes,
        output_content_type=str(payload.get('output_content_type') or ''),
        metadata=metadata,
        actual_cost=actual_cost,
        error_code=str(payload.get('error_code') or '')[:100],
    )


def _claim_provider_checkpoint_for_apply(
    job: MediaProcessingJob,
) -> tuple[MediaProcessingJob, uuid.UUID | None]:
    claimed_at = now()
    with transaction.atomic():
        locked = MediaProcessingJob.objects.select_for_update().get(pk=job.pk)
        if locked.provider_response_state in {
            MediaProcessingJob.ProviderResponseState.APPLIED,
            MediaProcessingJob.ProviderResponseState.ACCOUNTING_RESOLVED,
        }:
            _copy_provider_checkpoint_state(job, locked)
            return locked, None
        if (
            locked.provider_response_state
            == MediaProcessingJob.ProviderResponseState.APPLYING
            and locked.provider_response_apply_claimed_at is not None
            and locked.provider_response_apply_claimed_at
            > claimed_at - _PROVIDER_RESPONSE_APPLY_LEASE
        ):
            raise MediaProviderCheckpointApplyInProgress(
                'Durable provider response уже применяется другим worker.',
            )
        if locked.provider_response_state not in {
            MediaProcessingJob.ProviderResponseState.RECORDED,
            MediaProcessingJob.ProviderResponseState.APPLYING,
        }:
            raise MediaProviderCheckpointNotApplicable(
                'Для задачи нет известного ответа провайдера.',
            )

        apply_token = uuid.uuid4()
        locked.provider_response_state = (
            MediaProcessingJob.ProviderResponseState.APPLYING
        )
        locked.provider_response_apply_token = apply_token
        locked.provider_response_apply_claimed_at = claimed_at
        locked.save(update_fields=[
            'provider_response_state', 'provider_response_apply_token',
            'provider_response_apply_claimed_at', 'updated_at',
        ])
        _copy_provider_checkpoint_state(job, locked)
        return locked, apply_token


def _release_provider_checkpoint_apply_claim(job_id: int, apply_token: uuid.UUID) -> None:
    MediaProcessingJob.objects.filter(
        pk=job_id,
        provider_response_state=MediaProcessingJob.ProviderResponseState.APPLYING,
        provider_response_apply_token=apply_token,
    ).update(
        provider_response_state=MediaProcessingJob.ProviderResponseState.RECORDED,
        provider_response_apply_token=None,
        provider_response_apply_claimed_at=None,
        updated_at=now(),
    )


def _assert_checkpoint_accounting_compatible(
    job: MediaProcessingJob,
    result: MediaProviderResult,
) -> None:
    credit_state = _credit_state(job)
    if (
        result.status in {
            MediaProviderResultStatus.PENDING,
            MediaProviderResultStatus.SUCCEEDED,
        }
        and _reservation_from_state(credit_state) is not None
        and credit_state.get('status') == 'released'
    ):
        raise MediaProviderOutcomeUncertain(
            'Известный принятый ответ противоречит уже освобождённому резерву.',
        )
    if (
        result.status == MediaProviderResultStatus.FAILED
        and _reservation_from_state(credit_state) is not None
        and credit_state.get('status') == 'settled'
    ):
        raise MediaProviderOutcomeUncertain(
            'Известный отказ провайдера противоречит уже списанному резерву.',
        )


def apply_checkpointed_provider_result(job: MediaProcessingJob) -> MediaProcessingJob:
    """Apply one claimed durable response without invoking the provider again."""
    checkpoint, apply_token = _claim_provider_checkpoint_for_apply(job)
    if apply_token is None:
        job.refresh_from_db()
        return job
    try:
        result = _provider_result_from_checkpoint(checkpoint)
        _assert_checkpoint_accounting_compatible(checkpoint, result)
        applied = apply_provider_result(job, result)
    except Exception:
        _release_provider_checkpoint_apply_claim(job.pk, apply_token)
        raise

    with transaction.atomic():
        locked = MediaProcessingJob.objects.select_for_update().get(pk=job.pk)
        if (
            locked.provider_response_state
            != MediaProcessingJob.ProviderResponseState.APPLYING
            or locked.provider_response_apply_token != apply_token
        ):
            raise MediaProviderOutcomeUncertain(
                'Claim применения checkpoint был потерян до фиксации результата.',
            )
        locked.provider_response_enc = None
        locked.provider_response_state = (
            MediaProcessingJob.ProviderResponseState.APPLIED
        )
        locked.provider_response_apply_token = None
        locked.provider_response_apply_claimed_at = None
        locked.provider_response_resolved_at = now()
        locked.save(update_fields=[
            'provider_response_enc', 'provider_response_state',
            'provider_response_apply_token',
            'provider_response_apply_claimed_at',
            'provider_response_resolved_at', 'updated_at',
        ])
        _copy_provider_checkpoint_state(applied, locked)
    return applied


def mark_provider_checkpoint_accounting_resolved(job: MediaProcessingJob) -> None:
    """Explicitly abandon local apply after an operator resolves accounting."""
    with transaction.atomic():
        locked = MediaProcessingJob.objects.select_for_update().get(pk=job.pk)
        if (
            locked.provider_response_state
            == MediaProcessingJob.ProviderResponseState.APPLYING
        ):
            raise MediaProviderCheckpointApplyInProgress(
                'Checkpoint сейчас применяется; accounting reconciliation запрещена.',
            )
        if (
            locked.provider_response_state
            == MediaProcessingJob.ProviderResponseState.RECORDED
        ):
            locked.provider_response_enc = None
            locked.provider_response_state = (
                MediaProcessingJob.ProviderResponseState.ACCOUNTING_RESOLVED
            )
            locked.provider_response_apply_token = None
            locked.provider_response_apply_claimed_at = None
            locked.provider_response_resolved_at = now()
            locked.save(update_fields=[
                'provider_response_enc', 'provider_response_state',
                'provider_response_apply_token',
                'provider_response_apply_claimed_at',
                'provider_response_resolved_at', 'updated_at',
            ])
        _copy_provider_checkpoint_state(job, locked)


def submit_job(job: MediaProcessingJob, callback_url: str = '') -> MediaProcessingJob:
    checkpoint = MediaProcessingJob.objects.only(
        'provider_job_id', 'provider_response_enc', 'provider_response_digest',
        'provider_response_status', 'provider_response_state',
        'provider_response_recorded_at', 'provider_response_apply_token',
        'provider_response_apply_claimed_at',
        'provider_response_resolved_at',
    ).get(pk=job.pk)
    _copy_provider_checkpoint_state(job, checkpoint)
    if checkpoint.provider_response_state:
        return apply_checkpointed_provider_result(job)

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
    except Exception:
        # Nothing was sent to the provider, so releasing and retrying is safe.
        _release_job_credits(job, reason='pre_provider_submission_failed')
        raise

    try:
        result = provider.process(MediaProviderRequest(
            input_url=input_url,
            operations=operations,
            parameters=job.parameters,
            callback_url=callback_url,
            idempotency_key=job.idempotency_key,
        ))
    except Exception as exc:
        # The provider may have accepted the operation before the response was
        # lost. Keep credits reserved and require reconciliation; neither the
        # worker nor an API retry may submit the operation again.
        raise MediaProviderOutcomeUncertain(
            'Результат отправки медиа-провайдеру неизвестен.',
        ) from exc
    try:
        _checkpoint_provider_result(job, result)
        return apply_checkpointed_provider_result(job)
    except Exception as exc:
        # The provider boundary has already been crossed.  A local S3, DB or
        # wallet failure must never turn into another provider submission: the
        # remote operation may have completed and been billed successfully.
        raise MediaProviderOutcomeUncertain(
            'Провайдер вернул ответ, но локальное сохранение '
            'не завершилось; требуется сверка.',
        ) from exc


def apply_provider_result(
    job: MediaProcessingJob,
    result: MediaProviderResult,
) -> MediaProcessingJob:
    _assert_checkpoint_accounting_compatible(job, result)
    job.provider_job_id = result.provider_job_id or job.provider_job_id
    job.provider_metadata = {**(job.provider_metadata or {}), **result.metadata}
    if result.actual_cost is not None:
        job.provider_metadata['provider_actual_cost'] = str(result.actual_cost)

    if result.status == MediaProviderResultStatus.PENDING:
        _settle_job_credits(job)
        job.status = MediaProcessingJob.Status.SUBMITTED
        job.error_code = ''
        job.error_message = ''
        job.save(update_fields=[
            'provider_job_id', 'provider_metadata', 'charged_credits',
            'status', 'error_code', 'error_message', 'updated_at',
        ])
        return job

    if result.status == MediaProviderResultStatus.FAILED:
        _release_job_credits(job, reason=result.error_code or 'provider_error')
        return fail_job(
            job,
            result.error_code or 'provider_error',
            _GENERIC_PROVIDER_FAILURE,
        )

    # A SUCCEEDED provider response is already beyond the billable boundary.
    # Download, validation, S3 and DB failures are not proof that the provider
    # rejected/refunded the operation, so let submit_job classify them as an
    # uncertain outcome while keeping the tenant reservation held.
    raw_bytes, content_type = _provider_result_bytes(result)
    variant = _store_variant(job, raw_bytes, content_type)

    _settle_job_credits(job)
    job.status = MediaProcessingJob.Status.SUCCEEDED
    job.finished_at = now()
    job.error_code = ''
    job.error_message = ''
    job.provider_metadata = {**job.provider_metadata, 'variant_id': variant.pk}
    job.save(update_fields=[
        'provider_job_id', 'provider_metadata', 'charged_credits',
        'status', 'finished_at', 'error_code', 'error_message', 'updated_at',
    ])
    return job


def fail_job(job: MediaProcessingJob, error_code: str, error_message: str) -> MediaProcessingJob:
    job.status = MediaProcessingJob.Status.FAILED
    job.error_code = error_code[:100]
    # Never persist signed provider URLs from transport exceptions. Provider
    # messages are user-visible through the job serializer, so keep them short
    # and strip controls even for internal callers.
    safe_message = _URL_IN_ERROR_RE.sub('[redacted-url]', str(error_message or ''))
    job.error_message = ''.join(
        char for char in safe_message if char in {'\n', '\t'} or ord(char) >= 32
    )[:1000]
    job.finished_at = now()
    job.save(update_fields=[
        'status', 'error_code', 'error_message', 'finished_at', 'updated_at',
    ])
    return job


def fail_job_if_checkpoint_unresolved(
    job: MediaProcessingJob,
    error_code: str,
    error_message: str,
) -> bool:
    """Mark failure only while no newer apply/accounting owner has won.

    A stale worker may discover that its apply token was replaced after it has
    completed local work. Re-reading without a row lock would let that loser
    overwrite the winner's APPLIED/SUCCEEDED state.
    """
    with transaction.atomic():
        locked = MediaProcessingJob.objects.select_for_update().get(pk=job.pk)
        if (
            locked.provider_response_state in {
                MediaProcessingJob.ProviderResponseState.APPLYING,
                MediaProcessingJob.ProviderResponseState.APPLIED,
                MediaProcessingJob.ProviderResponseState.ACCOUNTING_RESOLVED,
            }
            or locked.status in {
                MediaProcessingJob.Status.SUBMITTED,
                MediaProcessingJob.Status.SUCCEEDED,
                MediaProcessingJob.Status.CANCELLED,
            }
        ):
            job.status = locked.status
            job.error_code = locked.error_code
            job.error_message = locked.error_message
            _copy_provider_checkpoint_state(job, locked)
            return False
        fail_job(locked, error_code, error_message)
        job.status = locked.status
        job.error_code = locked.error_code
        job.error_message = locked.error_message
        return True


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
        parsed_output_url = urlparse(str(result.output_url))
        if (
            parsed_output_url.scheme.lower() != 'https'
            or not parsed_output_url.hostname
            or parsed_output_url.username is not None
            or parsed_output_url.password is not None
        ):
            raise ValueError('Результат провайдера должен использовать HTTPS URL.')
        try:
            response = request_public_http_url(
                result.output_url,
                timeout=30,
                max_response_bytes=settings.MEDIA_PROVIDER_OUTPUT_MAX_BYTES,
                redirect_policy=REDIRECT_SAME_ORIGIN,
            )
            response.raise_for_status()
        except Exception as exc:
            raise ValueError(_GENERIC_PROVIDER_OUTPUT_FAILURE) from exc
        content_type = response.headers.get('Content-Type', '').split(';', 1)[0]
        raw_bytes = response.content
    else:
        raise ValueError('Провайдер не вернул изображение.')

    if not raw_bytes or len(raw_bytes) > settings.MEDIA_PROVIDER_OUTPUT_MAX_BYTES:
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
        validate_image_pixel_budget(image)
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError('Результат провайдера не удалось открыть как изображение.') from exc

    sha = hashlib.sha256(raw_bytes).hexdigest()
    extension = _extension_for_content_type(content_type, result_url='')
    original_path = PurePosixPath(job.product_image.s3_key)
    variant_key = str(
        original_path.with_name(
            f'{original_path.stem}_variant_{job.pk}_{sha[:12]}{extension}',
        ),
    )
    with transaction.atomic():
        locked_image = ProductImage.objects.select_for_update().get(
            pk=job.product_image_id,
        )
        existing = ProductImageVariant.objects.filter(
            product_image=locked_image,
            sha256=sha,
        ).first()
        if existing:
            return existing

        saved_key = default_storage.save(variant_key, io.BytesIO(raw_bytes))
        try:
            return ProductImageVariant.objects.create(
                tenant=job.tenant,
                product_image=locked_image,
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
        except Exception:
            delete_storage_keys((saved_key,), storage=default_storage)
            raise


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
