"""Реестр источников изображений.

Регистрация через декоратор @register.
Получение активных через get_active_sources(product).
"""

import logging

from django.conf import settings

from apps.image_search.sources.base import BaseImageSource
from apps.image_search.sources.connection import image_source_connection

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, type[BaseImageSource]] = {}


def register(cls: type[BaseImageSource]):
    """Декоратор-регистратор источника.

    Использование:
        @register
        class AutodocSource(BaseImageSource):
            source_id = "autodoc"
            ...
    """
    if not cls.source_id:
        raise ValueError(f'{cls.__name__} должен иметь source_id')
    _REGISTRY[cls.source_id] = cls
    logger.debug(f'Зарегистрирован источник: {cls.source_id} (tier={cls.tier})')
    return cls


def get_active_sources(product) -> list[BaseImageSource]:
    """Возвращает активные и доступные источники, отсортированные по tier.

    Список активных источников берётся из settings.IMAGE_SOURCES_ENABLED.

    Args:
        product: экземпляр Product для инициализации источников.

    Returns:
        Список экземпляров источников, готовых к поиску.
    """
    enabled = getattr(settings, 'IMAGE_SOURCES_ENABLED', [])
    sources = []
    for source_id in enabled:
        cls = _REGISTRY.get(source_id)
        if cls is None:
            logger.warning(f'Источник {source_id!r} включён в настройках, но не зарегистрирован')
            continue
        instance = cls(product)
        if instance.is_available():
            sources.append(instance)
        else:
            logger.debug(f'Источник {source_id!r} недоступен, пропускаем')
    # The same primary/fallback choice configured for internet research also
    # controls image search. Tier remains a trust signal, not a routing switch.
    return sorted(
        sources,
        key=lambda source: (
            image_source_connection(
                source.source_id, getattr(product, 'tenant', None),
            ).priority,
            source.tier,
        ),
    )


def get_registered_sources() -> dict[str, type[BaseImageSource]]:
    """Возвращает все зарегистрированные источники (для диагностики)."""
    return dict(_REGISTRY)
