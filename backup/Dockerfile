FROM postgres:16.14-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777

RUN apk add --no-cache age ca-certificates python3

ENV PATH="/opt/backup-venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    BACKUP_STATE_DIR=/state

WORKDIR /opt/app
COPY backup/requirements.txt /opt/app/backup/requirements.txt
RUN set -eux; \
    apk add --no-cache --virtual .backup-python-installer py3-pip; \
    /usr/bin/python3 -m venv --without-pip /opt/backup-venv; \
    venv_site_packages="$(/opt/backup-venv/bin/python -c \
        'import sysconfig; print(sysconfig.get_path("purelib"))')"; \
    ensurepip_dir="$(/usr/bin/python3 -c \
        'import ensurepip, os; print(os.path.dirname(ensurepip.__file__))')"; \
    /usr/bin/python3 -m pip install \
        --no-cache-dir --disable-pip-version-check \
        --require-hashes --only-binary=:all: \
        --target "$venv_site_packages" \
        -r /opt/app/backup/requirements.txt; \
    apk del .backup-python-installer; \
    rm -rf "$ensurepip_dir"; \
    command -v age >/dev/null; \
    command -v pg_dump >/dev/null; \
    ! apk info --exists py3-pip; \
    ! apk info --exists py3-setuptools; \
    ! /usr/bin/python3 -c 'import ensurepip'; \
    ! find /usr/lib /opt/backup-venv -type f \
        \( -path '*/pip/_vendor/msgpack/*' \
        -o -path '*/pip/_vendor/setuptools/*' \
        -o -name 'pip-*.whl' \) -print -quit | grep -q .; \
    /opt/backup-venv/bin/python -c \
        "import importlib.util; import boto3, cryptography, psycopg; assert importlib.util.find_spec('pip') is None; assert importlib.util.find_spec('setuptools') is None"

COPY backup /opt/app/backup
RUN mkdir -p /state \
    && chown -R postgres:postgres /state /opt/app

USER postgres
STOPSIGNAL SIGTERM
CMD ["python3", "-m", "backup.create"]
