"""Тесты скорера качества изображений."""

import pytest

from apps.image_search.services.quality import score
from apps.image_search.sources.base import ImageCandidate


def _make_candidate(tier=4, confidence='HIGH', width=0, height=0):
    return ImageCandidate(
        url='https://example.com/img.jpg',
        source_id='duckduckgo',
        tier=tier,
        width=width,
        height=height,
        raw_meta={'confidence': confidence},
    )


class TestScore:
    """Тесты функции score."""

    def test_high_confidence_tier4_без_разрешения(self):
        # 0.50 (HIGH) + 0.10 (tier4) = 0.60
        assert score(_make_candidate(tier=4, confidence='HIGH')) == 0.60

    def test_high_confidence_tier1_без_разрешения(self):
        # 0.50 (HIGH) + 0.40 (tier1) = 0.90
        assert score(_make_candidate(tier=1, confidence='HIGH')) == 0.90

    def test_хорошее_разрешение_даёт_бонус_0_20(self):
        without_res = score(_make_candidate(tier=4, confidence='HIGH'))
        with_res = score(_make_candidate(tier=4, confidence='HIGH', width=1280, height=960))
        assert with_res - without_res == pytest.approx(0.20)

    def test_среднее_разрешение_даёт_бонус_0_10(self):
        without_res = score(_make_candidate(tier=4, confidence='HIGH'))
        mid_res = score(_make_candidate(tier=4, confidence='HIGH', width=400, height=300))
        assert mid_res - without_res == pytest.approx(0.10)

    def test_низкое_разрешение_ниже_300_не_даёт_бонус(self):
        without_res = score(_make_candidate(tier=4, confidence='HIGH'))
        low_res = score(_make_candidate(tier=4, confidence='HIGH', width=200, height=200))
        assert low_res == without_res

    def test_скор_не_превышает_1(self):
        c = _make_candidate(tier=1, confidence='HIGH', width=1920, height=1080)
        assert score(c) <= 1.0

    def test_неизвестный_confidence_даёт_минимальный_вклад(self):
        # 0.05 (unknown) + 0.10 (tier4) = 0.15
        assert score(_make_candidate(tier=4, confidence='UNKNOWN')) == pytest.approx(0.15)

    def test_medium_confidence_tier4_без_разрешения(self):
        # 0.30 (MEDIUM) + 0.10 (tier4) = 0.40 — ниже MIN_QUALITY_SCORE=0.45
        assert score(_make_candidate(tier=4, confidence='MEDIUM')) == 0.40

    def test_medium_confidence_с_хорошим_разрешением_проходит_порог(self):
        # 0.30 + 0.10 + 0.20 = 0.60 — выше MIN_QUALITY_SCORE=0.45
        s = score(_make_candidate(tier=4, confidence='MEDIUM', width=800, height=600))
        assert s == pytest.approx(0.60)

    def test_разрешение_ровно_600_даёт_большой_бонус(self):
        at_600 = score(_make_candidate(tier=4, confidence='HIGH', width=600, height=600))
        just_below = score(_make_candidate(tier=4, confidence='HIGH', width=599, height=599))
        assert at_600 > just_below
