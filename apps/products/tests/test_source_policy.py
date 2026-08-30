import pytest

from apps.products.source_policy import (
    DEFAULT_PART_SOURCE, get_part_source_config, get_part_source_policy,
    get_part_source_policies, should_auto_apply_fitment, should_auto_apply_record,
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


def test_registry_snapshot_is_available_for_parser_sources():
    policies = get_part_source_policies()

    assert DEFAULT_PART_SOURCE in policies
    assert policies[DEFAULT_PART_SOURCE].label == 'Tachka.ru'
    assert policies['euroauto'].label == 'Euroauto.ru'
    assert policies['euroauto'].transport == 'catalog_search'
    assert policies['euroauto'].capabilities.supports_fitments is True
    assert policies['euroauto'].capabilities.supports_images is True
    assert policies['euroauto'].capabilities.supports_related_parts is True


def test_unknown_source_records_are_not_auto_applied():
    class Record:
        source_id = 'unregistered-source'
        confidence = 1.0
        needs_review = False

    assert should_auto_apply_record(Record()) is False


def test_review_status_overrides_auto_apply_policy():
    class ApprovedRecord:
        source_id = 'unregistered-source'
        confidence = 0.1
        needs_review = True
        review_status = 'approved'

    class RejectedRecord:
        source_id = DEFAULT_PART_SOURCE
        confidence = 1.0
        needs_review = False
        review_status = 'rejected'

    assert should_auto_apply_record(ApprovedRecord()) is True
    assert should_auto_apply_record(RejectedRecord()) is False


def test_fitments_require_human_approval_before_auto_apply():
    class PendingTenantFitment:
        source_id = DEFAULT_PART_SOURCE
        model = 'E-CLASS'
        confidence = 1.0
        needs_review = False
        review_status = 'pending'

    class ApprovedTenantFitment(PendingTenantFitment):
        review_status = 'approved'

    class ParserGlobalFitment:
        source_id = DEFAULT_PART_SOURCE
        model = 'E-CLASS'
        confidence = 1.0
        needs_review = False

    class HumanGlobalFitment(ParserGlobalFitment):
        source_id = 'human_review'

    assert should_auto_apply_fitment(PendingTenantFitment()) is False
    assert should_auto_apply_fitment(ApprovedTenantFitment()) is True
    assert should_auto_apply_fitment(ParserGlobalFitment()) is False
    assert should_auto_apply_fitment(HumanGlobalFitment()) is True
