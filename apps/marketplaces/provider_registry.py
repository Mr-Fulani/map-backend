from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ProviderCapabilities:
    """Provider behavior available to shared marketplace flows."""

    account_health: bool = False
    catalog_schema: bool = False
    publication_preflight: bool = False
    publish_or_update: bool = False
    price_update: bool = False
    stock_update: bool = False
    archive: bool = False
    status_reconcile: bool = False
    statistics: bool = False
    feed_delivery: bool = False
    placement_addresses: bool = False

    def supports(self, capability: str) -> bool:
        return bool(getattr(self, capability, False))

    def public_contract(self) -> dict[str, bool]:
        """Expose neutral names plus M1a compatibility aliases."""
        contract = asdict(self)
        contract.update({
            'publication': self.publish_or_update,
            'status_check': self.status_reconcile,
            'analytics': self.statistics,
        })
        return contract


AVITO_CAPABILITIES = ProviderCapabilities(
    account_health=True,
    catalog_schema=True,
    publication_preflight=True,
    publish_or_update=True,
    price_update=True,
    stock_update=True,
    archive=True,
    status_reconcile=True,
    statistics=True,
    feed_delivery=True,
    placement_addresses=True,
)
OZON_CAPABILITIES = ProviderCapabilities(
    account_health=True,
    catalog_schema=True,
)

_PROVIDERS = {
    'avito': AVITO_CAPABILITIES,
    'ozon': OZON_CAPABILITIES,
}
_UNSUPPORTED_PROVIDER = ProviderCapabilities()


def provider_capabilities(marketplace: str) -> ProviderCapabilities:
    """Return a fail-closed capability set for a provider identifier."""
    return _PROVIDERS.get(marketplace, _UNSUPPORTED_PROVIDER)


class ProviderCapabilityUnavailable(ValueError):
    """The selected provider does not implement the requested operation."""

    def __init__(self, marketplace: str, capability: str):
        super().__init__(
            f'Операция недоступна для маркетплейса {marketplace or "unknown"}.',
        )
        self.marketplace = marketplace
        self.capability = capability


def require_provider_capability(marketplace: str, capability: str) -> None:
    if not provider_capabilities(marketplace).supports(capability):
        raise ProviderCapabilityUnavailable(marketplace, capability)
