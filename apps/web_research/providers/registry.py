from apps.web_research.providers.base import BaseWebSearchProvider


_PROVIDERS: dict[str, type[BaseWebSearchProvider]] = {}


def register_search_provider(cls: type[BaseWebSearchProvider]):
    if not cls.provider_id:
        raise ValueError('Web search provider must define provider_id')
    _PROVIDERS[cls.provider_id] = cls
    return cls


def get_search_provider(provider_id: str = '') -> BaseWebSearchProvider | None:
    if provider_id:
        cls = _PROVIDERS.get(provider_id)
        instance = cls() if cls else None
        return instance if instance and instance.is_available() else None
    for cls in _PROVIDERS.values():
        instance = cls()
        if instance.is_available():
            return instance
    return None


def create_search_provider(
    provider_id: str, *, credentials: dict | None = None,
    parameters: dict | None = None,
) -> BaseWebSearchProvider | None:
    cls = _PROVIDERS.get((provider_id or '').strip().lower())
    return cls(credentials=credentials, parameters=parameters) if cls else None


def registered_search_providers() -> dict[str, type[BaseWebSearchProvider]]:
    return dict(_PROVIDERS)
