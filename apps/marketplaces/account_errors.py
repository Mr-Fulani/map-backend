class AccountAlreadyExists(Exception):
    """Аккаунт с таким provider identity уже существует у тенанта."""

    def __init__(self, message: str, *, account_id: int | None = None):
        super().__init__(message)
        self.account_id = account_id


class InvalidMarketplaceCredentials(Exception):
    """Credentials маркетплейса не прошли проверку через API."""


class MarketplaceConnectionError(InvalidMarketplaceCredentials):
    """Нормализованная ошибка read-only проверки подключения провайдера."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


class MarketplaceProviderDisabled(Exception):
    """Provider package exists in code but its rollout flag is disabled."""
