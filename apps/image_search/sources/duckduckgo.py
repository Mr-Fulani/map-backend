"""Источник изображений: DuckDuckGo (Tier 4, бесплатный).

Использует пакет ddgs, который обрабатывает VQD-токены
и изменения API автоматически.

Глобальный кулдаун (Redis):
  ddg:ratelimit   — устанавливается на 5 мин при получении 403. Все последующие
                    запросы пропускаются немедленно, не нагружая DuckDuckGo.
  ddg:session     — устанавливается на 60 сек перед каждой сессией (cache.add).
                    Если ключ уже есть — другая задача сейчас работает с DDG,
                    пропускаем чтобы не получить новый бан.
"""

import logging
import time

from django.core.cache import cache

from apps.image_search.sources.base import BaseImageSource, ImageCandidate
from apps.image_search.sources.registry import register

logger = logging.getLogger(__name__)

try:
    from ddgs import DDGS
except ImportError:
    DDGS = None  # type: ignore[assignment,misc]

_MAX_RESULTS_PER_QUERY = 15
_MAX_QUERIES = 2
_QUERY_DELAY_SEC = 1.5
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SEC = 3.0

# Redis-кулдауны
_RATELIMIT_KEY = 'ddg:ratelimit'
_RATELIMIT_TTL = 300       # 5 минут после 403
_SESSION_KEY = 'ddg:session'
_SESSION_TTL = 60          # 60 секунд между сессиями


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
    max_queries = _MAX_QUERIES

    def search(self) -> list[ImageCandidate]:
        """Ищет изображения по данным товара через DuckDuckGo Images.

        Берёт первые _MAX_QUERIES запросов из build_queries() и агрегирует
        кандидатов, избегая дублей по URL.

        Returns:
            Список ImageCandidate с width/height из API и confidence в raw_meta.
        """
        if DDGS is None:
            logger.error('[ddg] пакет ddgs не установлен')
            return []

        # Глобальный бан после 403 — ждать пока TTL не истечёт
        if cache.get(_RATELIMIT_KEY):
            logger.info('[ddg] IP в бане, пропускаем (кулдаун ~%d мин)', _RATELIMIT_TTL // 60)
            return []

        # Межсессионный кулдаун — только одна сессия DDG в 60 секунд
        # cache.add() атомарно устанавливает ключ ТОЛЬКО если его нет → предотвращает гонку
        if not cache.add(_SESSION_KEY, 1, timeout=_SESSION_TTL):
            logger.info('[ddg] глобальный кулдаун между сессиями (%d с), пропускаем', _SESSION_TTL)
            return []

        queries = self.build_queries()[:self.max_queries]
        candidates: list[ImageCandidate] = []
        seen_urls: set[str] = set()

        for idx, (query, confidence) in enumerate(queries):
            if idx > 0:
                time.sleep(_QUERY_DELAY_SEC)
            results = self._search_with_retry(query)
            for rank, r in enumerate(results, start=1):
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
                        'query': query,
                        'rank': rank,
                    },
                ))

        return candidates

    def _search_with_retry(self, query: str) -> list[dict]:
        """Выполняет поиск с повторными попытками при rate-limit ошибках."""
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                with DDGS() as ddgs:
                    return list(ddgs.images(
                        query,
                        backend='duckduckgo',
                        region='ru-ru',
                        max_results=_MAX_RESULTS_PER_QUERY,
                    ))
            except Exception as exc:
                is_last = attempt == _RETRY_ATTEMPTS - 1
                exc_str = str(exc)

                # При 403 ставим глобальный бан-кулдаун
                if '403' in exc_str or 'Ratelimit' in exc_str:
                    cache.set(_RATELIMIT_KEY, 1, timeout=_RATELIMIT_TTL)
                    logger.warning(
                        '[ddg] 403 Ratelimit для %r — бан на %d мин, дальнейшие запросы пропущены',
                        query, _RATELIMIT_TTL // 60,
                    )
                    return []

                if is_last:
                    logger.warning('[ddg] ошибка для %r: %s', query, exc)
                    return []
                wait = _RETRY_BACKOFF_SEC * (attempt + 1)
                logger.debug('[ddg] попытка %d для %r: %s, ждём %.1fs', attempt + 1, query, exc, wait)
                time.sleep(wait)
        return []
