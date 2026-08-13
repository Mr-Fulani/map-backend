"""Базовый класс для всех источников изображений.

Новый источник: наследуй BaseImageSource, реализуй search(), зарегистрируй через @register.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from apps.web_research.providers.base import WebSearchProviderError


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


class ImageSearchOutcomeUncertain(WebSearchProviderError):
    """The paid provider may have accepted a request before transport failed."""

    outcome_uncertain = True

    def __init__(self, message: str, *, code: str = 'outcome_uncertain') -> None:
        super().__init__(
            message,
            retryable=False,
            code=code,
            outcome_uncertain=True,
        )


class BaseImageSource(ABC):
    """Базовый класс для всех источников изображений.

    Новый источник: наследуй, реализуй search(), зарегистрируй через @register.
    """

    source_id: str = ''               # Уникальный идентификатор: "autodoc"
    tier: int = 99                    # Приоритет (меньше = выше)
    is_free: bool = True              # False = платный API
    requires_key: bool = False        # Нужен ли API-ключ
    max_queries: int = 1              # Сколько первых запросов источник реально выполняет

    @property
    def provider_id(self) -> str:
        """Compatibility with the shared paid web-search accounting ledger."""
        return self.source_id

    def __init__(
        self,
        product,
        *,
        web_search_workflow=None,
        workflow_plan: dict | None = None,
        consumed_attempt_ids: set[int] | None = None,
    ) -> None:
        """Инициализация с товаром для поиска.

        Args:
            product: экземпляр Product (поля brand, article, name).
        """
        self.product = product
        self.last_error = ''
        self.last_error_code = ''
        self.last_attempt_query = ''
        self.web_search_workflow = web_search_workflow
        self.workflow_plan = workflow_plan
        # Every paid slot (including a durable safe failure) must be included
        # in the exact workflow ACK.  All sources in one pipeline share this
        # collector so a crash/replay can prove which checkpoints were used.
        self.consumed_attempt_ids = (
            consumed_attempt_ids if consumed_attempt_ids is not None else set()
        )

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

    def planned_queries(self) -> list[tuple[str, str]]:
        """Return the immutable queries persisted for this workflow owner."""
        if self.workflow_plan is None:
            return self.build_queries()[:self.max_queries]
        raw_queries = self.workflow_plan.get('queries')
        if not isinstance(raw_queries, list):
            raise ValueError('image-search workflow query plan is invalid')
        queries: list[tuple[str, str]] = []
        for raw in raw_queries[:self.max_queries]:
            if not isinstance(raw, dict):
                raise ValueError('image-search workflow query is invalid')
            query = raw.get('query')
            confidence = raw.get('confidence')
            if not isinstance(query, str) or not isinstance(confidence, str):
                raise ValueError('image-search workflow query is invalid')
            queries.append((query, confidence))
        return queries

    def build_workflow_plan(self, *, source_index: int) -> dict:
        """Build the non-secret immutable portion common to every source."""
        connection_id = None
        if not self.is_free:
            from apps.image_search.sources.connection import image_source_connection

            connection = image_source_connection(
                self.source_id,
                getattr(self.product, 'tenant', None),
            )
            connection_id = getattr(connection.database_connection, 'pk', None)
        return {
            'source_id': self.source_id,
            'source_index': int(source_index),
            'is_free': bool(self.is_free),
            # This is public routing identity, not a credential.  Persisting it
            # prevents a deleted/recreated admin connection from silently
            # receiving an old workflow's not-yet-started paid slot.
            'connection_id': connection_id,
            'queries': [
                {'query': query, 'confidence': confidence}
                for query, confidence in self.build_queries()[:self.max_queries]
            ],
        }

    def consume_attempt(self, attempt_id: object) -> None:
        if isinstance(attempt_id, int) and not isinstance(attempt_id, bool):
            self.consumed_attempt_ids.add(attempt_id)
