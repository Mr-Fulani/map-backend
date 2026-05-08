from abc import ABC, abstractmethod
from datetime import datetime


class BaseDataSourceAdapter(ABC):
    """Базовый класс для всех адаптеров источников данных (1С HTTP/XML, CSV)."""

    def __init__(self, connection):
        self.connection = connection

    @abstractmethod
    def fetch_changes(self, since: datetime, limit: int = 500, offset: int = 0) -> list[dict]:
        """Возвращает список изменённых записей в нормализованном формате."""

    @abstractmethod
    def test_connection(self) -> bool:
        """Проверяет доступность источника. Возвращает True или бросает исключение."""

    @abstractmethod
    def get_display_name(self) -> str:
        """Человекочитаемое название адаптера."""
