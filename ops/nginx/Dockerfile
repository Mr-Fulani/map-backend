FROM nginx:1.30.4-alpine@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46

RUN set -eux; \
    apk add --no-cache --upgrade libcrypto3 libssl3; \
    for package in libcrypto3 libssl3; do \
        version="$(apk list --installed "$package" \
            | sed -n "s/^${package}-\\([^ ]*\\).*/\\1/p")"; \
        [ -n "$version" ]; \
        comparison="$(apk version -t "$version" 3.5.8-r0)"; \
        [ "$comparison" = '=' ] || [ "$comparison" = '>' ]; \
    done
