from dataclasses import dataclass, field


DEFAULT_PART_SOURCE = 'tachka'
AUTO_APPLY_MIN_CONFIDENCE = 0.85


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
        }


PART_SOURCE_POLICIES = {
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
}


def get_part_source_policy(source_id: str) -> PartSourcePolicy:
    if source_id not in PART_SOURCE_POLICIES:
        raise ValueError(f'Unknown part parser source: {source_id}')
    return PART_SOURCE_POLICIES[source_id]


def get_part_source_config(source_id: str) -> dict:
    return get_part_source_policy(source_id).as_legacy_config()


def should_auto_apply_relation(relation) -> bool:
    if getattr(relation, 'needs_review', False):
        return False
    if getattr(relation, 'relation_type', '') == 'Unknown':
        return False
    return getattr(relation, 'confidence', 0.0) >= _min_confidence(relation)


def should_auto_apply_fitment(fitment) -> bool:
    if getattr(fitment, 'needs_review', False):
        return False
    if not getattr(fitment, 'model', ''):
        return False
    return getattr(fitment, 'confidence', 0.0) >= _min_confidence(fitment)


def _min_confidence(record) -> float:
    source_id = getattr(record, 'source_id', '') or DEFAULT_PART_SOURCE
    try:
        return get_part_source_policy(source_id).auto_apply_min_confidence
    except ValueError:
        return AUTO_APPLY_MIN_CONFIDENCE
