import pytest

from apps.products.source_policy import (
    DEFAULT_PART_SOURCE, get_part_source_config, get_part_source_policy,
)


def test_tachka_source_policy_exposes_capabilities_and_limits():
    policy = get_part_source_policy(DEFAULT_PART_SOURCE)

    assert policy.source_id == 'tachka'
    assert policy.transport == 'httpx'
    assert policy.trust_score == pytest.approx(0.85)
    assert policy.capabilities.supports_product_page is True
    assert policy.capabilities.supports_search is True
    assert policy.capabilities.supports_fitments is True
    assert policy.auto_apply_min_confidence == pytest.approx(0.85)


def test_legacy_source_config_keeps_existing_bulk_action_contract():
    config = get_part_source_config(DEFAULT_PART_SOURCE)

    assert config['batch_size'] == 20
    assert config['min_pause_seconds'] == 10
    assert config['default_pause_seconds'] == 60
    assert config['capabilities']['supports_related_parts'] is True


def test_unknown_source_policy_is_rejected():
    with pytest.raises(ValueError):
        get_part_source_policy('missing-source')
