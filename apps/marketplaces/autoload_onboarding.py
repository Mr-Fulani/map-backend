"""Recoverable, tenant-visible Avito Autoload onboarding state."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from django.db import transaction
from django.utils import timezone

from apps.marketplaces.models import (
    AvitoAccountStatus,
    MarketplaceAccount,
    MarketplaceFeedEndpoint,
)


ERROR_PREFIX = 'autoload_onboarding_'
STATE_KEY = 'autoload_onboarding'
DISPATCH_FAILED = f'{ERROR_PREFIX}dispatch_failed'
RETRYING = f'{ERROR_PREFIX}retrying'
EXHAUSTED = f'{ERROR_PREFIX}exhausted'
MANUAL_REVIEW = f'{ERROR_PREFIX}manual_review'


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AutoloadOnboardingPresentation:
    state: str
    profile_state: str
    ready: bool
    retryable: bool
    message: str


def _status_for_account(account: MarketplaceAccount) -> AvitoAccountStatus:
    status, _ = AvitoAccountStatus.objects.get_or_create(
        account=account,
        defaults={'tenant_id': account.tenant_id},
    )
    return status


def record_autoload_onboarding_state(
    account: MarketplaceAccount,
    *,
    code: str,
    message: str,
) -> None:
    """Persist bounded tenant-safe state without overwriting Avito health."""

    if not code.startswith(ERROR_PREFIX):
        raise ValueError('Autoload onboarding code is outside its namespace.')
    status = _status_for_account(account)
    attempted_at = timezone.now()
    notification_state = dict(status.notification_state or {})
    notification_state[STATE_KEY] = {
        'code': code[:50],
        'message': ' '.join(str(message).split())[:500],
        'attempted_at': attempted_at.isoformat(),
    }
    status.notification_state = notification_state
    status.last_attempted_at = attempted_at
    status.save(update_fields=(
        'last_attempted_at',
        'notification_state',
        'updated_at',
    ))


def touch_autoload_onboarding_attempt(account: MarketplaceAccount) -> None:
    status = _status_for_account(account)
    status.last_attempted_at = timezone.now()
    status.save(update_fields=('last_attempted_at', 'updated_at'))


def clear_autoload_onboarding_state(account: MarketplaceAccount) -> None:
    status = _status_for_account(account)
    notification_state = dict(status.notification_state or {})
    if STATE_KEY not in notification_state:
        return
    notification_state.pop(STATE_KEY, None)
    status.notification_state = notification_state
    status.save(update_fields=(
        'notification_state',
        'updated_at',
    ))


def _persisted_state(status: AvitoAccountStatus | None) -> tuple[str, str]:
    if status is None:
        return '', ''
    value = (status.notification_state or {}).get(STATE_KEY)
    if not isinstance(value, dict):
        return '', ''
    code = str(value.get('code') or '')
    message = str(value.get('message') or '')
    if not code.startswith(ERROR_PREFIX):
        return '', ''
    return code, message


def mark_autoload_onboarding_manual_review(account_id: int) -> None:
    """Fence an unsafe provider profile so scanners cannot replay its POST."""

    with transaction.atomic():
        account = (
            MarketplaceAccount.all_objects.select_for_update(of=('self',))
            .filter(pk=account_id)
            .first()
        )
        if account is None:
            return
        endpoint = (
            MarketplaceFeedEndpoint.objects.select_for_update(of=('self',))
            .filter(account_id=account.pk)
            .first()
        )
        if (
            endpoint is not None
            and endpoint.profile_state
            != MarketplaceFeedEndpoint.ProfileState.MANUAL_REVIEW
        ):
            endpoint.profile_state = MarketplaceFeedEndpoint.ProfileState.MANUAL_REVIEW
            endpoint.profile_revision += 1
            endpoint.save(update_fields=(
                'profile_state',
                'profile_revision',
                'updated_at',
            ))
        record_autoload_onboarding_state(
            account,
            code=MANUAL_REVIEW,
            message=(
                'Профиль Avito нельзя безопасно изменить автоматически. '
                'Нужна ручная проверка поддержки.'
            ),
        )


def schedule_autoload_profile_setup(
    account_id: int,
    tenant_id: int,
    *,
    countdown: int | None = None,
) -> bool:
    """Publish onboarding work without letting a broker error escape HTTP."""

    try:
        from apps.marketplaces.tasks import setup_autoload_profile_task

        if countdown is None:
            setup_autoload_profile_task.delay(account_id, tenant_id)
        else:
            setup_autoload_profile_task.apply_async(
                args=[account_id, tenant_id],
                countdown=max(0, int(countdown)),
            )
        return True
    except Exception:
        logger.exception(
            'Unable to dispatch Avito Autoload onboarding account_id=%s '
            'tenant_id=%s',
            account_id,
            tenant_id,
        )
        account = (
            MarketplaceAccount.objects.select_related('tenant')
            .filter(pk=account_id, tenant_id=tenant_id)
            .first()
        )
        if account is not None:
            record_autoload_onboarding_state(
                account,
                code=DISPATCH_FAILED,
                message=(
                    'Подключение сохранено, но фоновая настройка пока не '
                    'запущена. MAP повторит попытку автоматически.'
                ),
            )
        return False


def autoload_onboarding_presentation(
    account: MarketplaceAccount,
) -> AutoloadOnboardingPresentation:
    """Return a read-only tenant-facing summary from existing DB evidence."""

    try:
        endpoint = account.feed_endpoint
    except MarketplaceFeedEndpoint.DoesNotExist:
        endpoint = None
    try:
        status = account.avito_status
    except AvitoAccountStatus.DoesNotExist:
        status = None

    if endpoint is None:
        code, persisted_message = _persisted_state(status)
        if code in {DISPATCH_FAILED, RETRYING, EXHAUSTED}:
            return AutoloadOnboardingPresentation(
                state='exhausted' if code == EXHAUSTED else 'retrying',
                profile_state='',
                ready=False,
                retryable=code == EXHAUSTED,
                message=(
                    persisted_message
                    or 'MAP повторит настройку подключения автоматически.'
                ),
            )
        return AutoloadOnboardingPresentation(
            state='legacy',
            profile_state='',
            ready=bool(status and status.feed_configured),
            retryable=False,
            message='Подключение использует legacy Autoload-профиль.',
        )

    profile_state = endpoint.profile_state
    code, persisted_message = _persisted_state(status)
    if (
        profile_state == MarketplaceFeedEndpoint.ProfileState.VERIFIED
        and endpoint.serve_enabled
    ):
        return AutoloadOnboardingPresentation(
            state='ready',
            profile_state=profile_state,
            ready=True,
            retryable=False,
            message='Защищённый feed endpoint подтверждён в Avito.',
        )
    if profile_state == MarketplaceFeedEndpoint.ProfileState.MANUAL_REVIEW:
        return AutoloadOnboardingPresentation(
            state='manual_review',
            profile_state=profile_state,
            ready=False,
            retryable=False,
            message=(
                persisted_message
                if persisted_message
                else 'Нужна ручная проверка Autoload-профиля.'
            ),
        )

    if code == EXHAUSTED:
        return AutoloadOnboardingPresentation(
            state='exhausted',
            profile_state=profile_state,
            ready=False,
            retryable=True,
            message=(
                persisted_message
                or 'Автоматические попытки исчерпаны. Запустите повтор вручную.'
            ),
        )
    if profile_state == MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN:
        return AutoloadOnboardingPresentation(
            state='reconciling',
            profile_state=profile_state,
            ready=False,
            retryable=True,
            message='MAP сверяет, принял ли Avito изменение профиля.',
        )
    if code in {DISPATCH_FAILED, RETRYING}:
        return AutoloadOnboardingPresentation(
            state='retrying',
            profile_state=profile_state,
            ready=False,
            retryable=False,
            message=(
                persisted_message
                or 'MAP повторит настройку подключения автоматически.'
            ),
        )
    return AutoloadOnboardingPresentation(
        state='pending',
        profile_state=profile_state,
        ready=False,
        retryable=False,
        message='MAP настраивает защищённый feed endpoint в Avito.',
    )
