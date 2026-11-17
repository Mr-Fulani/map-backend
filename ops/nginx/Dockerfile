FROM nginx:1.30.6-alpine@sha256:901e593c95dc85f5a729f5765516321366ef4ac401821431d408897e95287d0a

RUN set -eux; \
    apk add --no-cache --upgrade libcrypto3 libssl3 libexpat 'libuuid>=2.42.3-r0' \
        'libxslt>=1.1.43-r4'; \
    for package in libcrypto3 libssl3; do \
        version="$(apk list --installed "$package" \
            | sed -n "s/^${package}-\\([^ ]*\\).*/\\1/p")"; \
        [ -n "$version" ]; \
        comparison="$(apk version -t "$version" 3.5.8-r0)"; \
        [ "$comparison" = '=' ] || [ "$comparison" = '>' ]; \
    done; \
    version="$(apk list --installed libexpat \
        | sed -n 's/^libexpat-\([^ ]*\).*/\1/p')"; \
    [ -n "$version" ]; \
    comparison="$(apk version -t "$version" 2.8.4-r0)"; \
    [ "$comparison" = '=' ] || [ "$comparison" = '>' ]
