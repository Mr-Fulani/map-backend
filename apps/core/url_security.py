import ipaddress
from urllib.parse import urlparse


def is_safe_public_http_url(value: str) -> bool:
    """Reject malformed URLs and explicit local/private network destinations."""
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
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return True
        return not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )
    except (TypeError, ValueError):
        return False
