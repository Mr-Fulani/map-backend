FROM postgres:16.14-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777

RUN set -eux; \
    apk add --no-cache --upgrade libcrypto3 libssl3; \
    for package in libcrypto3 libssl3; do \
        version="$(apk list --installed "$package" \
            | sed -n "s/^${package}-\\([^ ]*\\).*/\\1/p")"; \
        [ -n "$version" ]; \
        comparison="$(apk version -t "$version" 3.5.8-r0)"; \
        [ "$comparison" = '=' ] || [ "$comparison" = '>' ]; \
    done
