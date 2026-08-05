"""Tests for technical quality, kept independent from product identity."""

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
    def test_unknown_dimensions_are_reviewable(self):
        assert score(_make_candidate()) == 0.45

    def test_query_confidence_does_not_change_technical_quality(self):
        high = score(_make_candidate(confidence='HIGH'))
        low = score(_make_candidate(confidence='VERY_LOW'))
        assert high == low

    def test_source_tier_does_not_change_technical_quality(self):
        assert score(_make_candidate(tier=1)) == score(_make_candidate(tier=4))

    def test_large_image_has_high_quality_score(self):
        assert score(_make_candidate(width=1600, height=1200)) == 0.95

    def test_600px_image_has_good_quality_score(self):
        assert score(_make_candidate(width=800, height=600)) == 0.82

    def test_300px_image_has_usable_quality_score(self):
        assert score(_make_candidate(width=400, height=300)) == 0.65

    def test_small_correct_image_is_marked_for_processing_not_identity_rejection(self):
        assert score(_make_candidate(width=208, height=208)) == 0.35

    def test_tiny_image_has_low_quality_score(self):
        assert score(_make_candidate(width=100, height=100)) == 0.18

    def test_extreme_aspect_ratio_has_penalty(self):
        regular = score(_make_candidate(width=800, height=600))
        panoramic = score(_make_candidate(width=2000, height=600))
        assert panoramic < regular
