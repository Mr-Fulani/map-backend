import uuid
from decimal import Decimal

from django.conf import settings
from yookassa import Configuration
from yookassa import Payment as YkPayment


def _configure() -> None:
    """Устанавливает учётные данные YooKassa из настроек Django."""
    Configuration.account_id = settings.YOOKASSA_SHOP_ID
    Configuration.secret_key = settings.YOOKASSA_SECRET_KEY


def create_payment(
    amount: Decimal,
    description: str,
    return_url: str,
    metadata: dict,
) -> tuple[str, str]:
    """
    Создаёт платёж в YooKassa и возвращает (payment_id, confirmation_url).

    Идемпотентный ключ генерируется автоматически (UUID4).
    capture=True — автоматическое подтверждение без двухшагового захвата.
    """
    _configure()
    payment = YkPayment.create(
        {
            'amount': {'value': f'{amount:.2f}', 'currency': 'RUB'},
            'confirmation': {'type': 'redirect', 'return_url': return_url},
            'capture': True,
            'description': description,
            'metadata': metadata,
        },
        str(uuid.uuid4()),
    )
    return payment.id, payment.confirmation.confirmation_url
