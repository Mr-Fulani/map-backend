from abc import ABC, abstractmethod


class BaseMarketplaceAdapter(ABC):
    """
    Базовый адаптер маркетплейса.

    Два типа реализаций:
    - Feed-based (Avito, Auto.ru): publish/update/unpublish/delete работают через XML/JSON-фид,
      который загружается на публичный URL; Avito скачивает его асинхронно.
      flush_feed() загружает батч и возвращает признак успеха;
      get_feed_results() сопоставляет ad_id ↔ external_id после обработки.
    - REST-based (будущие маркетплейсы с прямым API): возвращают external_id синхронно из publish().
    """

    def __init__(self, account):
        self.account = account

    # --- Операции с отдельным объявлением ---

    @abstractmethod
    def publish(self, listing) -> str | None:
        """
        Публикует объявление.

        REST-based: возвращает external_id сразу.
        Feed-based: возвращает None; external_id придёт через get_feed_results().
        """

    @abstractmethod
    def update(self, listing) -> None:
        """Обновляет контент объявления (заголовок, описание)."""

    @abstractmethod
    def update_price(self, listing) -> None:
        """Обновляет только цену. Часто доступно через REST даже у feed-based адаптеров."""

    @abstractmethod
    def unpublish(self, listing) -> None:
        """Снимает объявление с публикации."""

    @abstractmethod
    def delete(self, listing) -> None:
        """Удаляет объявление."""

    @abstractmethod
    def get_status(self, listing) -> dict:
        """Возвращает текущий статус объявления на маркетплейсе."""

    # --- Feed-based адаптеры переопределяют эти методы ---

    def flush_feed(self, listings: list) -> bool:
        """
        Загружает пакет объявлений как фид на маркетплейс.

        Генерирует XML/JSON, загружает на S3, уведомляет маркетплейс.
        Возвращает True при успехе. REST-based адаптеры не переопределяют этот метод.
        """
        return False

    def get_feed_results(self, ad_ids: list[str]) -> list[dict]:
        """
        Запрашивает результаты обработки фида.

        Возвращает список: [{"ad_id": str, "avito_id": int | None}].
        REST-based адаптеры не переопределяют этот метод.
        """
        return []
