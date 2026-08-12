#!/usr/bin/env python3
"""Exercise Squid's final-destination ACL from an application container."""

import socket


PROXY_ADDRESS = ('egress_proxy', 3128)
BLOCKED_CONNECT_AUTHORITIES = (
    '127.0.0.1:443',
    'ci-private-target.dodugir.com:443',
    '[::2]:443',
    '[2001:2::1]:443',
    '[3fff::1]:443',
    '[400::1]:443',
    '[fc00::1]:443',
)


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
    print('Egress proxy private/special-use destination ACL: ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
