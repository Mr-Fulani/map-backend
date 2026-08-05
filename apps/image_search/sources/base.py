"""Базовый класс для всех источников изображений.

Новый источник: наследуй BaseImageSource, реализуй search(), зарегистрируй через @register.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ImageCandidate:
    """Кандидат-изображение, найденный источником."""

    url: str                          # Прямая ссылка на изображение
    source_id: str                    # "autodoc", "exist", "duckduckgo"
    tier: int                         # Приоритет источника (меньше = выше)
    is_free: bool = True              # False = платный API
    width: int = 0                    # Ширина (если известна)
    height: int = 0                   # Высота (если известна)
    quality_score: float = 0.0        # Заполняется QualityScorer
    phash: str = ''                   # Заполняется при скачивании
    raw_meta: dict = field(default_factory=dict)  # Доп. данные (confidence и т.д.)


class BaseImageSource(ABC):
    """Базовый класс для всех источников изображений.

    Новый источник: наследуй, реализуй search(), зарегистрируй через @register.
    """

    source_id: str = ''               # Уникальный идентификатор: "autodoc"
    tier: int = 99                    # Приоритет (меньше = выше)
    is_free: bool = True              # False = платный API
    requires_key: bool = False        # Нужен ли API-ключ
    max_queries: int = 1              # Сколько первых запросов источник реально выполняет

    def __init__(self, product):
        """Инициализация с товаром для поиска.

        Args:
            product: экземпляр Product (поля brand, article, name).
        """
        self.product = product
        self.last_error = ''
        self.last_error_code = ''

    @abstractmethod
    def search(self) -> list[ImageCandidate]:
        """Возвращает кандидатов в порядке убывания релевантности."""
        ...

    def is_available(self) -> bool:
        """True если источник сконфигурирован и доступен."""
        return True

    def build_queries(self) -> list[tuple[str, str]]:
        """Возвращает поисковые запросы.

        Переопределяй при нестандартной логике запросов.
        По умолчанию делегирует в query_builder.build_queries().
        """
        from apps.image_search.services.query_builder import build_queries
        return build_queries(self.product)
