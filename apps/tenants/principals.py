from dataclasses import dataclass

from apps.tenants.models import APIKey


@dataclass(frozen=True, slots=True)
class APIKeyPrincipal:
    """Least-privilege machine identity; deliberately not a Django User."""

    api_key_id: int
    tenant_id: int
    role: str
    scopes: frozenset[str]

    is_authenticated = True
    is_anonymous = False
    is_active = True
    is_staff = False
    is_superuser = False
    is_api_key = True

    @classmethod
    def from_api_key(cls, api_key: APIKey):
        return cls(
            api_key_id=api_key.pk,
            tenant_id=api_key.tenant_id,
            role=api_key.role,
            scopes=frozenset(api_key.scopes or []),
        )

    def has_scopes(self, required_scopes) -> bool:
        return frozenset(required_scopes).issubset(self.scopes)

    def can_write(self) -> bool:
        return self.role == APIKey.ROLE_OPERATOR

    def can_manage_billing(self) -> bool:
        return False

    def can_manage_users(self) -> bool:
        return False

    def can_manage_connections(self) -> bool:
        return False


def human_user_or_none(request):
    user = getattr(request, 'user', None)
    if (
        user is not None
        and getattr(user, 'is_authenticated', False)
        and not getattr(user, 'is_api_key', False)
    ):
        return user
    return None
