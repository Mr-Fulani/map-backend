from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_standalone_dashboard_cards_override_responsive_zero_top_padding():
    expected_counts = {
        'frontend/src/app/dashboard/research/page.tsx': 4,
        'frontend/src/app/dashboard/billing/page.tsx': 6,
        'frontend/src/app/dashboard/media/page.tsx': 1,
    }

    for relative_path, expected_count in expected_counts.items():
        source = (ROOT / relative_path).read_text(encoding='utf-8')
        assert source.count('sm:p-4') == expected_count

    billing = (
        ROOT / 'frontend/src/app/dashboard/billing/page.tsx'
    ).read_text(encoding='utf-8')
    # The payment-history card is intentionally flush. Its mobile p-0 must
    # also override CardContent's responsive sm:p-6/sm:pt-0 defaults.
    assert 'className="p-0 sm:p-0"' in billing


def test_media_header_cards_keep_header_aware_content_spacing():
    source = (
        ROOT / 'frontend/src/app/dashboard/media/page.tsx'
    ).read_text(encoding='utf-8')

    # Only the standalone KPI card gets symmetric padding. CardContent that
    # follows CardHeader must keep the component's intentional zero top edge.
    assert source.count('<CardHeader>') == 2
    assert source.count('sm:p-4') == 1
