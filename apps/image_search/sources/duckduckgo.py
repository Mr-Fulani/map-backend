"""Источник изображений: DuckDuckGo (Tier 4, бесплатный).

Использует пакет duckduckgo-search, который обрабатывает VQD-токены
и изменения API автоматически.
"""

import logging
import time

from apps.image_search.sources.base import BaseImageSource, ImageCandidate
from apps.image_search.sources.registry import register

logger = logging.getLogger(__name__)

try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None  # type: ignore[assignment,misc]

_MAX_RESULTS_PER_QUERY = 15
_MAX_QUERIES = 2
_QUERY_DELAY_SEC = 1.5
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SEC = 3.0


@register
class DuckDuckGoSource(BaseImageSource):
    """Источник изображений через DuckDuckGo Images.

    Tier 4 — fallback когда автозапчастные сайты не дали результата.
    Требует задержку между запросами для снижения риска блокировки.
    """

    source_id = 'duckduckgo'
    tier = 4
    is_free = True
    requires_key = False

    def search(self) -> list[ImageCandidate]:
        """Ищет изображения по данным товара через DuckDuckGo Images.

        Берёт первые _MAX_QUERIES запросов из build_queries() и агрегирует
        кандидатов, избегая дублей по URL.

        Returns:
            Список ImageCandidate с width/height из API и confidence в raw_meta.
        """
        if DDGS is None:
            logger.error('[ddg] пакет duckduckgo-search не установлен')
            return []

        queries = self.build_queries()[:_MAX_QUERIES]
        candidates: list[ImageCandidate] = []
        seen_urls: set[str] = set()

        for idx, (query, confidence) in enumerate(queries):
            if idx > 0:
                time.sleep(_QUERY_DELAY_SEC)
            results = self._search_with_retry(query)
            for r in results:
                url = r.get('image', '')
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                candidates.append(ImageCandidate(
                    url=url,
                    source_id=self.source_id,
                    tier=self.tier,
                    width=r.get('width', 0),
                    height=r.get('height', 0),
                    raw_meta={
                        'confidence': confidence,
                        'title': r.get('title', ''),
                    },
                ))

        return candidates

    def _search_with_retry(self, query: str) -> list[dict]:
        """Выполняет поиск с повторными попытками при rate-limit ошибках."""
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                with DDGS() as ddgs:
                    return list(ddgs.images(
                        keywords=query,
                        region='ru-ru',
                        max_results=_MAX_RESULTS_PER_QUERY,
                    ))
            except Exception as exc:
                is_last = attempt == _RETRY_ATTEMPTS - 1
                if is_last:
                    logger.warning(f'[ddg] ошибка для {query!r}: {exc}')
                    return []
                wait = _RETRY_BACKOFF_SEC * (attempt + 1)
                logger.debug(f'[ddg] попытка {attempt + 1} для {query!r}: {exc}, ждём {wait}s')
                time.sleep(wait)
        return []
