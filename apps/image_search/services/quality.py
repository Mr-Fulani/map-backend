"""Technical image-quality estimate, deliberately separate from product identity."""

from apps.image_search.sources.base import ImageCandidate


def score(candidate: ImageCandidate) -> float:
    """Estimate resolution/usability only; never decide whether it is the right part."""
    s = 0.45  # unknown dimensions are reviewable, not automatically bad
    if candidate.width and candidate.height:
        min_dim = min(candidate.width, candidate.height)
        max_dim = max(candidate.width, candidate.height)
        if min_dim >= 1000:
            s = 0.95
        elif min_dim >= 600:
            s = 0.82
        elif min_dim >= 300:
            s = 0.65
        elif min_dim >= 180:
            s = 0.35
        else:
            s = 0.18
        if min_dim and max_dim / min_dim > 3:
            s -= 0.15

    return min(max(s, 0.0), 1.0)
