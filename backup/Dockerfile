FROM postgres:16.14-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777

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
