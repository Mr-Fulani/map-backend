FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

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
