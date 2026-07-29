import ipaddress
import logging

logger = logging.getLogger(__name__)

# Официальные IP-адреса YooKassa для вебхуков
# https://yookassa.ru/developers/using-api/webhooks
YOOKASSA_IP_RANGES = [
    '185.71.76.0/27',
    '185.71.77.0/27',
    '77.75.153.0/25',
    '77.75.156.11/32',
    '77.75.156.35/32',
    '2a02:5180::/32',
]

_NETWORKS = [ipaddress.ip_network(r) for r in YOOKASSA_IP_RANGES]


def is_yookassa_ip(request) -> bool:
    """
    Проверяет, что запрос пришёл с официального IP YooKassa.

    Учитывает X-Forwarded-For при работе за реверс-прокси.
    """
    forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded_for:
        # nginx добавляет реальный адрес клиента в конец цепочки через
        # $proxy_add_x_forwarded_for; левую часть клиент может подделать.
        remote_ip = forwarded_for.split(',')[-1].strip()
    else:
        remote_ip = request.META.get('REMOTE_ADDR', '')

    try:
        addr = ipaddress.ip_address(remote_ip)
    except ValueError:
        logger.warning('YooKassa webhook: невалидный IP %s', remote_ip)
        return False

    for network in _NETWORKS:
        if addr in network:
            return True

    logger.warning('YooKassa webhook: запрос с неизвестного IP %s', remote_ip)
    return False
