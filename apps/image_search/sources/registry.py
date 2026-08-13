"""Реестр источников изображений.

Регистрация через декоратор @register.
Получение активных через get_active_sources(product).
"""

import logging

from django.conf import settings

from apps.image_search.sources.base import BaseImageSource, ImageSearchOutcomeUncertain
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


def get_active_sources(product, *, web_search_workflow=None) -> list[BaseImageSource]:
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
        instance = cls(product, web_search_workflow=web_search_workflow)
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


def build_image_search_workflow_snapshot(product) -> dict:
    """Freeze ordered providers, public request inputs and logical slots.

    Credentials are deliberately absent.  A retry uses this exact persisted
    plan, while current credentials are resolved only for a slot that has no
    durable checkpoint yet.
    """
    sources = get_active_sources(product)
    return {
        'version': 1,
        'kind': 'image_search',
        'product_id': product.pk,
        'tenant_id': product.tenant_id,
        'sources': [
            source.build_workflow_plan(source_index=index)
            for index, source in enumerate(sources)
        ],
    }


def get_workflow_sources(
    product,
    workflow,
    *,
    consumed_attempt_ids: set[int],
) -> list[BaseImageSource]:
    """Reconstruct sources from persisted input without availability routing."""
    snapshot = workflow.input_snapshot
    if (
        not isinstance(snapshot, dict)
        or snapshot.get('version') != 1
        or snapshot.get('kind') != 'image_search'
        or snapshot.get('product_id') != product.pk
        or snapshot.get('tenant_id') != product.tenant_id
        or not isinstance(snapshot.get('sources'), list)
    ):
        raise ImageSearchOutcomeUncertain(
            'Неизменяемый план поиска изображений повреждён; '
            'требуется сверка.',
            code='provider_request_conflict',
        )

    sources: list[BaseImageSource] = []
    for raw_plan in snapshot['sources'][:20]:
        if not isinstance(raw_plan, dict):
            raise ImageSearchOutcomeUncertain(
                'Неизменяемый план поиска изображений повреждён; '
                'требуется сверка.',
                code='provider_request_conflict',
            )
        source_id = raw_plan.get('source_id')
        if not isinstance(source_id, str) or source_id not in _REGISTRY:
            raise ImageSearchOutcomeUncertain(
                'Запланированный источник изображений недоступен '
                'в этой версии кода.',
                code='provider_adapter_missing',
            )
        source = _REGISTRY[source_id](
            product,
            web_search_workflow=workflow,
            workflow_plan=raw_plan,
            consumed_attempt_ids=consumed_attempt_ids,
        )
        sources.append(source)
    return sources
