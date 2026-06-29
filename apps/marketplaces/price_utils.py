from decimal import Decimal


def effective_margin(listing) -> Decimal:
    """Возвращает актуальную наценку для листинга.

    Приоритет: listing.margin_pct → category.default_margin_pct → 0.
    """
    if listing.margin_pct is not None:
        return listing.margin_pct
    cat = getattr(listing.product, 'catalog_category', None)
    if cat is not None:
        return cat.default_margin_pct
    return Decimal('0')


def compute_price(base_price: Decimal, margin_pct: Decimal) -> Decimal:
    """Возвращает цену с учётом наценки: base_price * (1 + margin_pct / 100)."""
    return (base_price * (1 + margin_pct / 100)).quantize(Decimal('0.01'))
