"""Источник изображений: DuckDuckGo (Tier 4, бесплатный).

Использует неофициальный API DuckDuckGo Image Search через httpx.
Не требует API-ключа, но нестабилен (rate limit, изменения API).
Используется как fallback когда автозапчастные сайты не дали результата.
"""

import logging
import re
import time

import httpx

from apps.image_search.sources.base import BaseImageSource, ImageCandidate
from apps.image_search.sources.registry import register

logger = logging.getLogger(__name__)

_DDG_BASE_URL = 'https://duckduckgo.com'
_DDG_IMAGES_URL = 'https://duckduckgo.com/i.js'
_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/122.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8',
}
_MAX_RESULTS_PER_QUERY = 15
_MAX_QUERIES = 2
_QUERY_DELAY_SEC = 1.5


@register
class DuckDuckGoSource(BaseImageSource):
    """Источник изображений через неофициальный API DuckDuckGo.

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
        queries = self.build_queries()[:_MAX_QUERIES]
        candidates: list[ImageCandidate] = []
        seen_urls: set[str] = set()

        try:
            with httpx.Client(
                headers=_HEADERS,
                timeout=10,
                follow_redirects=True,
            ) as client:
                for idx, (query, confidence) in enumerate(queries):
                    if idx > 0:
                        time.sleep(_QUERY_DELAY_SEC)
                    try:
                        vqd = self._get_vqd(client, query)
                        if not vqd:
                            logger.debug(f'[ddg] vqd не найден для запроса: {query!r}')
                            continue
                        results = self._fetch_images(client, query, vqd)
                    except httpx.HTTPError as exc:
                        logger.warning(f'[ddg] HTTP ошибка для {query!r}: {exc}')
                        continue

                    for r in results[:_MAX_RESULTS_PER_QUERY]:
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
        except Exception as exc:
            logger.error(f'[ddg] неожиданная ошибка: {exc}', exc_info=True)

        return candidates

    def _get_vqd(self, client: httpx.Client, query: str) -> str:
        """Получает VQD-токен — обязательный параметр для запроса к i.js.

        DuckDuckGo генерирует токен на своей главной странице.
        Без него i.js возвращает 403.

        Args:
            client: httpx.Client с нужными заголовками.
            query: Поисковый запрос.

        Returns:
            VQD-токен или пустую строку если не найден.
        """
        resp = client.get(
            _DDG_BASE_URL + '/',
            params={'q': query, 'iax': 'images', 'ia': 'images'},
        )
        resp.raise_for_status()
        # Токен встречается как vqd=3-abc или vqd='3-abc' — пропускаем кавычку
        match = re.search(r"vqd=['\"]?([^'\"&<\s]+)", resp.text)
        return match.group(1) if match else ''

    def _fetch_images(
        self, client: httpx.Client, query: str, vqd: str,
    ) -> list[dict]:
        """Запрашивает список изображений через i.js endpoint.

        Args:
            client: httpx.Client.
            query: Поисковый запрос.
            vqd: VQD-токен полученный через _get_vqd.

        Returns:
            Список dict с полями image, thumbnail, title, width, height.
        """
        resp = client.get(
            _DDG_IMAGES_URL,
            params={
                'q': query,
                'vqd': vqd,
                'o': 'json',
                'p': '1',
                'l': 'ru-ru',
                'f': ',,,,,',
            },
            headers={**_HEADERS, 'Referer': _DDG_BASE_URL + '/'},
        )
        resp.raise_for_status()
        return resp.json().get('results', [])
