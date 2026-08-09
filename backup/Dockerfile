FROM postgres:18.4-alpine@sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15

RUN apk add --no-cache age ca-certificates python3 py3-pip \
    && python3 -m venv /opt/backup-venv

ENV PATH="/opt/backup-venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    BACKUP_STATE_DIR=/state

WORKDIR /opt/app
COPY backup/requirements.txt /opt/app/backup/requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
        --require-hashes --only-binary=:all: \
        -r /opt/app/backup/requirements.txt

COPY backup /opt/app/backup
RUN mkdir -p /state \
    && chown -R postgres:postgres /state /opt/app

USER postgres
STOPSIGNAL SIGTERM
CMD ["python3", "-m", "backup.create"]
