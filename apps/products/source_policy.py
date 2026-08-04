from dataclasses import dataclass, field


DEFAULT_PART_SOURCE = 'tachka'
AUTO_APPLY_MIN_CONFIDENCE = 0.85
AUTO_APPLY_MIN_TRUST_SCORE = 0.75
REVIEW_STATUS_APPROVED = 'approved'
REVIEW_STATUS_REJECTED = 'rejected'


@dataclass(frozen=True)
class SourceCapabilities:
    supports_product_page: bool = True
    supports_search: bool = False
    supports_fitments: bool = False
    supports_images: bool = False
    supports_related_parts: bool = False


@dataclass(frozen=True)
class PartSourcePolicy:
    source_id: str
    label: str
    priority: int
    trust_score: float
    default_pause_seconds: int
    min_pause_seconds: int
    batch_size: int
    transport: str = 'httpx'
    capabilities: SourceCapabilities = field(default_factory=SourceCapabilities)
    auto_apply_min_confidence: float = AUTO_APPLY_MIN_CONFIDENCE
    auto_apply_min_trust_score: float = AUTO_APPLY_MIN_TRUST_SCORE

    def as_legacy_config(self) -> dict:
        return {
            'label': self.label,
            'priority': self.priority,
            'trust_score': self.trust_score,
            'default_pause_seconds': self.default_pause_seconds,
            'min_pause_seconds': self.min_pause_seconds,
            'batch_size': self.batch_size,
            'transport': self.transport,
            'capabilities': {
                'supports_product_page': self.capabilities.supports_product_page,
                'supports_search': self.capabilities.supports_search,
                'supports_fitments': self.capabilities.supports_fitments,
                'supports_images': self.capabilities.supports_images,
                'supports_related_parts': self.capabilities.supports_related_parts,
            },
            'auto_apply_min_confidence': self.auto_apply_min_confidence,
            'auto_apply_min_trust_score': self.auto_apply_min_trust_score,
        }


PART_SOURCE_POLICIES = {
    'human_review': PartSourcePolicy(
        source_id='human_review',
        label='Подтверждено пользователем',
        priority=1000,
        trust_score=1.0,
        default_pause_seconds=0,
        min_pause_seconds=0,
        batch_size=100,
        capabilities=SourceCapabilities(
            supports_product_page=False,
            supports_fitments=True,
        ),
    ),
    'tachka': PartSourcePolicy(
        source_id='tachka',
        label='Tachka.ru',
        priority=100,
        trust_score=0.85,
        default_pause_seconds=60,
        min_pause_seconds=10,
        batch_size=20,
        transport='httpx',
        capabilities=SourceCapabilities(
            supports_product_page=True,
            supports_search=True,
            supports_fitments=True,
            supports_images=True,
            supports_related_parts=True,
        ),
    ),
    'rossko': PartSourcePolicy(
        source_id='rossko',
        label='Rossko.ru',
        priority=90,
        trust_score=0.85,
        default_pause_seconds=60,
        min_pause_seconds=10,
        batch_size=20,
        transport='httpx',
        capabilities=SourceCapabilities(
            supports_product_page=False,
            supports_search=True,
            supports_fitments=True,
            supports_images=True,
            supports_related_parts=False,
        ),
    ),
}


def get_part_source_policy(source_id: str) -> PartSourcePolicy:
    if source_id not in PART_SOURCE_POLICIES:
        raise ValueError(f'Unknown part parser source: {source_id}')
    return PART_SOURCE_POLICIES[source_id]


def get_part_source_config(source_id: str) -> dict:
    return get_part_source_policy(source_id).as_legacy_config()


def get_part_source_policies() -> dict[str, PartSourcePolicy]:
    return dict(PART_SOURCE_POLICIES)


def should_auto_apply_record(record) -> bool:
    review_status = getattr(record, 'review_status', '')
    if review_status == REVIEW_STATUS_REJECTED:
        return False
    if review_status == REVIEW_STATUS_APPROVED:
        return True
    if getattr(record, 'needs_review', False):
        return False
    source_id = getattr(record, 'source_id', '') or DEFAULT_PART_SOURCE
    policy = _policy_or_default(source_id)
    confidence = getattr(record, 'confidence', 0.0)
    return (
        policy.trust_score >= policy.auto_apply_min_trust_score
        and confidence >= policy.auto_apply_min_confidence
    )


def should_auto_apply_relation(relation) -> bool:
    if getattr(relation, 'relation_type', '') == 'Unknown':
        return False
    return should_auto_apply_record(relation)


def should_auto_apply_fitment(fitment) -> bool:
    if not getattr(fitment, 'model', ''):
        return False
    return should_auto_apply_record(fitment)


def should_mark_needs_review(record, has_conflict: bool = False) -> bool:
    if has_conflict:
        return True
    return not should_auto_apply_record(record)


def can_raise_confidence(current_source_id: str, incoming_source_id: str) -> bool:
    current_policy = _policy_or_default(current_source_id)
    incoming_policy = _policy_or_default(incoming_source_id)
    return (
        incoming_policy.trust_score > current_policy.trust_score
        or (
            incoming_policy.trust_score == current_policy.trust_score
            and incoming_policy.priority >= current_policy.priority
        )
    )


def has_conflicting_fact(existing_facts, incoming) -> bool:
    incoming_value_hash = getattr(incoming, 'value_hash', '')
    for fact in existing_facts:
        if getattr(fact, 'source_id', '') == getattr(incoming, 'source_id', ''):
            continue
        if getattr(fact, 'fact_type', '') != getattr(incoming, 'fact_type', ''):
            continue
        if getattr(fact, 'name', '') != getattr(incoming, 'name', ''):
            continue
        if getattr(fact, 'value_hash', '') != incoming_value_hash:
            return True
    return False


def has_conflicting_fitment(existing_fitments, incoming) -> bool:
    for fitment in existing_fitments:
        if getattr(fitment, 'source_id', '') == getattr(incoming, 'source_id', ''):
            continue
        if _fitment_identity(fitment) != _fitment_identity(incoming):
            continue
        if (
            getattr(fitment, 'date_from', '') != getattr(incoming, 'date_from', '')
            or getattr(fitment, 'date_to', '') != getattr(incoming, 'date_to', '')
        ):
            return True
    return False


def _policy_or_default(source_id: str) -> PartSourcePolicy:
    if not source_id:
        return get_part_source_policy(DEFAULT_PART_SOURCE)
    try:
        return get_part_source_policy(source_id)
    except ValueError:
        return PartSourcePolicy(
            source_id=source_id,
            label=source_id,
            priority=0,
            trust_score=0.0,
            default_pause_seconds=60,
            min_pause_seconds=10,
            batch_size=1,
            capabilities=SourceCapabilities(),
        )


def _fitment_identity(fitment) -> tuple:
    return (
        getattr(fitment, 'make', ''),
        getattr(fitment, 'model', ''),
        getattr(fitment, 'generation', ''),
        getattr(fitment, 'modification', ''),
        getattr(fitment, 'engine_code', ''),
        getattr(fitment, 'power_hp', None),
    )
