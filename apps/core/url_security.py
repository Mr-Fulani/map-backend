import ipaddress
import socket
from urllib.parse import urlparse


def _is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def is_safe_public_http_url(value: str, *, resolve_hostname: bool = False) -> bool:
    """Отклоняет некорректные URL и назначения в локальных/приватных сетях."""
    try:
        parsed = urlparse(str(value or '').strip())
        hostname = (parsed.hostname or '').rstrip('.').lower()
        if parsed.scheme not in {'http', 'https'} or not hostname:
            return False
        if parsed.username or parsed.password:
            return False
        if hostname == 'localhost' or hostname.endswith('.localhost'):
            return False
        try:
            return _is_public_address(hostname)
        except ValueError:
            if not resolve_hostname:
                return True

        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        }
        return bool(addresses) and all(_is_public_address(address) for address in addresses)
    except (OSError, TypeError, ValueError):
        return False
