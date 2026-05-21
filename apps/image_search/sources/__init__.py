"""Pluggable-источники изображений.

Каждый источник наследует BaseImageSource и регистрируется через @register.
Добавление нового источника: создать файл, наследовать, декоратор @register.
"""

from apps.image_search.sources.registry import get_active_sources, register  # noqa: F401
