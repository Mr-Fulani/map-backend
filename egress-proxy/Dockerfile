FROM ubuntu/squid:6.6-24.04_edge@sha256:8a3baed477e2c282ab8aa5edad442f69873246964f225c5c2ae8364b6610963c

USER root
RUN set -eux; \
    apt-get update; \
    apt-get install --yes --no-install-recommends --only-upgrade \
        apt \
        libapt-pkg6.0t64 \
        libssl3t64 \
        openssl; \
    for package in libssl3t64 openssl; do \
        version="$(dpkg-query -W -f='${Version}' "$package")"; \
        dpkg --compare-versions "$version" ge 3.0.13-0ubuntu3.11; \
    done; \
    for package in apt libapt-pkg6.0t64; do \
        version="$(dpkg-query -W -f='${Version}' "$package")"; \
        dpkg --compare-versions "$version" ge 2.8.4ubuntu0.1; \
    done; \
    rm -rf /var/lib/apt/lists/*
