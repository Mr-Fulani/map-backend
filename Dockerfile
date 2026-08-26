FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN set -eux; \
    apt-get update; \
    apt-get install --yes --no-install-recommends --only-upgrade \
        bsdutils \
        libblkid1 \
        liblastlog2-2 \
        libmount1 \
        libsmartcols1 \
        libssl3t64 \
        libuuid1 \
        login \
        mount \
        openssl \
        openssl-provider-legacy \
        util-linux; \
    for package in \
        libblkid1 \
        liblastlog2-2 \
        libmount1 \
        libsmartcols1 \
        libuuid1 \
        mount \
        util-linux; do \
        version="$(dpkg-query -W -f='${Version}' "$package")"; \
        dpkg --compare-versions "$version" ge 2.41.5-0+deb13u1; \
    done; \
    bsdutils_version="$(dpkg-query -W -f='${Version}' bsdutils)"; \
    dpkg --compare-versions \
        "$bsdutils_version" ge 1:2.41.5-0+deb13u1; \
    login_version="$(dpkg-query -W -f='${Version}' login)"; \
    dpkg --compare-versions \
        "$login_version" ge 1:4.16.0-2+really2.41.5-0+deb13u1; \
    for package in libssl3t64 openssl openssl-provider-legacy; do \
        version="$(dpkg-query -W -f='${Version}' "$package")"; \
        dpkg --compare-versions "$version" ge 3.5.7-1~deb13u2; \
    done; \
    rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /usr/sbin/nologin --uid 1000 app

WORKDIR /app

ARG REQUIREMENTS=requirements/dev.txt
COPY requirements/ requirements/
RUN case "$REQUIREMENTS" in \
        requirements/dev.txt|requirements/prod.txt) ;; \
        *) echo "Unsupported requirements file: $REQUIREMENTS" >&2; exit 2 ;; \
    esac \
    && python -m pip install --no-cache-dir --disable-pip-version-check \
        --require-hashes --only-binary=:all: \
        -r "$REQUIREMENTS"

COPY --chown=app:app . .
RUN mkdir -p /app/staticfiles \
    && chown app:app /app/staticfiles

USER app

EXPOSE 8000

STOPSIGNAL SIGTERM

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
