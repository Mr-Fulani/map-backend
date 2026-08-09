FROM postgres:16-alpine

RUN apk add --no-cache age ca-certificates python3 py3-pip \
    && python3 -m venv /opt/backup-venv

ENV PATH="/opt/backup-venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    BACKUP_STATE_DIR=/state

WORKDIR /opt/app
COPY backup/requirements.txt /opt/app/backup/requirements.txt
RUN pip install --no-cache-dir -r /opt/app/backup/requirements.txt

COPY backup /opt/app/backup
RUN mkdir -p /state \
    && chown -R postgres:postgres /state /opt/app

USER postgres
CMD ["python3", "-m", "backup.create"]
