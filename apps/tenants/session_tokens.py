"""Atomic JWT refresh rotation and session revocation primitives."""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.utils import datetime_from_epoch

from apps.tenants.models import TenantUser


_POSTGRES_BIGINT_MAX = (1 << 63) - 1


def _invalid_refresh() -> InvalidToken:
    return InvalidToken('Refresh token недействителен или уже использован.')


def _decode_refresh(raw_token: str) -> RefreshToken:
    if not isinstance(raw_token, str) or not raw_token:
        raise _invalid_refresh()
    try:
        return RefreshToken(raw_token)
    except TokenError as exc:
        raise _invalid_refresh() from exc


def _claim_id(token, name: str) -> int:
    value = token.get(name)
    if isinstance(value, bool):
        raise _invalid_refresh()
    if isinstance(value, int):
        claim_id = value
    elif (
        isinstance(value, str)
        and 1 <= len(value) <= 20
        and value.isascii()
        and value.isdecimal()
        and not (len(value) > 1 and value.startswith('0'))
    ):
        claim_id = int(value)
    else:
        raise _invalid_refresh()
    if not 0 < claim_id <= _POSTGRES_BIGINT_MAX:
        raise _invalid_refresh()
    return claim_id


def _browser_session_id(token, fallback_jti: str) -> str:
    """Return a stable, non-secret identifier for one refresh-token chain."""
    value = token.get('sid')
    if value is None:
        # Tokens issued before the browser-session-id rollout use their current
        # signed JTI as the chain identifier on the first rotation.
        return fallback_jti
    if (
        not isinstance(value, str)
        or not 16 <= len(value) <= 128
        or not all(
            character.isascii()
            and (character.isalnum() or character in '_-')
            for character in value
        )
    ):
        raise _invalid_refresh()
    return value


def _lock_valid_refresh(raw_token: str, *, expected_user_id: int | None = None):
    """Return (refresh, locked user, locked outstanding row) after all checks."""
    refresh = _decode_refresh(raw_token)
    user_id = _claim_id(refresh, api_settings.USER_ID_CLAIM)
    tenant_id = _claim_id(refresh, 'tenant_id')
    auth_version = _claim_id(refresh, 'auth_version')

    if expected_user_id is not None and user_id != expected_user_id:
        raise _invalid_refresh()

    User = get_user_model()
    user = User.objects.select_for_update().filter(pk=user_id, is_active=True).first()
    if user is None or user.auth_version != auth_version:
        raise _invalid_refresh()

    jti = refresh.get(api_settings.JTI_CLAIM)
    if not isinstance(jti, str) or not jti:
        raise _invalid_refresh()
    outstanding = OutstandingToken.objects.select_for_update().filter(jti=jti).first()
    if outstanding is None or outstanding.user_id != user.pk:
        raise _invalid_refresh()
    # RefreshToken проверяет blacklist при декодировании. Эта повторная проверка
    # выполняется уже после row lock и закрывает гонку двух refresh-запросов.
    if BlacklistedToken.objects.filter(token=outstanding).exists():
        raise _invalid_refresh()

    if not TenantUser.objects.filter(
        user=user,
        tenant_id=tenant_id,
        tenant__is_active=True,
    ).exists():
        raise _invalid_refresh()

    return refresh, user, outstanding, _browser_session_id(refresh, jti)


def rotate_refresh_token(raw_token: str) -> dict[str, str]:
    """Consume one refresh exactly once and persist its rotated successor."""
    with transaction.atomic():
        refresh, user, outstanding, browser_session_id = _lock_valid_refresh(raw_token)
        BlacklistedToken.objects.create(token=outstanding)

        refresh['sid'] = browser_session_id
        refresh.set_jti()
        refresh.set_exp()
        refresh.set_iat()
        rotated = str(refresh)
        OutstandingToken.objects.create(
            user=user,
            jti=refresh[api_settings.JTI_CLAIM],
            token=rotated,
            created_at=timezone.now(),
            expires_at=datetime_from_epoch(refresh['exp']),
        )

        return {
            'access': str(refresh.access_token),
            'refresh': rotated,
            'browser_session_id': browser_session_id,
        }


def blacklist_refresh_token(raw_token: str, *, expected_user_id: int | None = None) -> None:
    """Consume a current refresh token without issuing a replacement."""
    with transaction.atomic():
        _, _, outstanding, _ = _lock_valid_refresh(
            raw_token,
            expected_user_id=expected_user_id,
        )
        BlacklistedToken.objects.create(token=outstanding)


def _revoke_locked_user(user) -> None:
    user.auth_version += 1
    user.save(update_fields=['auth_version'])
    outstanding = OutstandingToken.objects.select_for_update().filter(
        user=user,
        expires_at__gt=timezone.now(),
    )
    blacklisted_ids = set(
        BlacklistedToken.objects.filter(token__in=outstanding).values_list(
            'token_id',
            flat=True,
        )
    )
    BlacklistedToken.objects.bulk_create(
        [
            BlacklistedToken(token=token)
            for token in outstanding
            if token.pk not in blacklisted_ids
        ],
        ignore_conflicts=True,
    )


def revoke_all_user_sessions(user_id: int) -> int:
    """Invalidate every access/refresh token and return the new auth version."""
    User = get_user_model()
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        _revoke_locked_user(user)
        return user.auth_version


def revoke_all_sessions_from_refresh(raw_token: str) -> int:
    """Validate a browser refresh and invalidate every session of its user."""
    with transaction.atomic():
        _, user, _, _ = _lock_valid_refresh(raw_token)
        _revoke_locked_user(user)
        return user.auth_version
