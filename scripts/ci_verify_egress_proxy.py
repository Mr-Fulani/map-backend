#!/usr/bin/env python3
"""Exercise Squid's final-destination ACL from an application container."""

import socket


PROXY_ADDRESS = ('egress_proxy', 3128)
BLOCKED_CONNECT_AUTHORITIES = (
    '127.0.0.1:443',
    'ci-private-target.dodugir.com:443',
    '[2001:2::1]:443',
    '[3fff::1]:443',
    '[400::1]:443',
    '[fc00::1]:443',
)
# Squid normalizes the deprecated IPv4-compatible low IPv6 form before the
# tunnel is established. Depending on its IPv6 build/runtime it either matches
# the ACL (403) or fails routing before CONNECT (503); both are fail-closed.
LOW_IPV6_FAIL_CLOSED_AUTHORITY = '[::2]:443'


def _connect_status_line(authority: str) -> bytes:
    request = (
        f'CONNECT {authority} HTTP/1.1\r\n'
        f'Host: {authority}\r\n'
        'Connection: close\r\n\r\n'
    ).encode('ascii')
    with socket.create_connection(PROXY_ADDRESS, timeout=5) as connection:
        connection.sendall(request)
        return connection.recv(4096).split(b'\r\n', 1)[0]


def main() -> int:
    for authority in BLOCKED_CONNECT_AUTHORITIES:
        status_line = _connect_status_line(authority)
        if b' 403 ' not in status_line:
            raise RuntimeError(
                f'egress proxy did not deny {authority}: {status_line!r}',
            )
        print(f'{authority}: {status_line.decode("ascii", errors="replace")}')
    status_line = _connect_status_line(LOW_IPV6_FAIL_CLOSED_AUTHORITY)
    if not any(code in status_line for code in (b' 403 ', b' 503 ')):
        raise RuntimeError(
            'egress proxy opened or unexpectedly handled low IPv6 '
            f'{LOW_IPV6_FAIL_CLOSED_AUTHORITY}: {status_line!r}',
        )
    print(
        f'{LOW_IPV6_FAIL_CLOSED_AUTHORITY}: '
        f'{status_line.decode("ascii", errors="replace")}',
    )
    print('Egress proxy private/special-use destination ACL: ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
