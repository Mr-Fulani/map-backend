#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [[ "${GITHUB_ACTIONS:-}" != "true" \
  || "${RUNNER_ENVIRONMENT:-}" != "github-hosted" \
  || -z "${RUNNER_TEMP:-}" ]]; then
  echo 'This smoke harness is restricted to an ephemeral GitHub-hosted Actions runner.' >&2
  exit 2
fi

export CI_CERTIFICATE_LIVE_DIR="$RUNNER_TEMP/saas-poster-cert/live/dodugir.com"
export CI_CERTIFICATE_ARCHIVE_DIR="$RUNNER_TEMP/saas-poster-cert/archive/dodugir.com"
COMPOSE=(
  docker compose
  -f docker-compose.prod.yml
  -f docker-compose.ci-runtime.yml
)

cleanup() {
  "${COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true
  rm -f \
    "$CI_CERTIFICATE_LIVE_DIR/privkey.pem" \
    "$CI_CERTIFICATE_LIVE_DIR/fullchain.pem"
  rmdir \
    "$CI_CERTIFICATE_LIVE_DIR" \
    "$CI_CERTIFICATE_ARCHIVE_DIR" \
    "$RUNNER_TEMP/saas-poster-cert/live" \
    "$RUNNER_TEMP/saas-poster-cert/archive" \
    "$RUNNER_TEMP/saas-poster-cert" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

set_env() {
  local key="$1"
  local value="$2"

  grep -q "^${key}=" .env \
    || { echo "Missing ${key} in .env.example" >&2; return 1; }
  sed -i "s|^${key}=.*|${key}=${value}|" .env
}

wait_healthy() {
  local service="$1"
  local attempt container_id state health

  for attempt in $(seq 1 60); do
    container_id="$("${COMPOSE[@]}" ps -q "$service")"
    if [[ -n "$container_id" ]]; then
      state="$(docker inspect --format '{{.State.Status}}' "$container_id")"
      health="$(
        docker inspect \
          --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
          "$container_id"
      )"
      if [[ "$state" == running && "$health" == healthy ]]; then
        return 0
      fi
      if [[ "$state" == exited || "$state" == dead ]]; then
        break
      fi
    fi
    sleep 2
  done

  "${COMPOSE[@]}" ps --all
  "${COMPOSE[@]}" logs --tail=200 "$service"
  echo "${service} did not become healthy" >&2
  return 1
}

export POSTGRES_DB=map_db
export POSTGRES_USER=map_user
export POSTGRES_PASSWORD=ci-production-db-password
export CACHE_REDIS_PASSWORD=ci-cache-password
export CELERY_REDIS_PASSWORD=ci-broker-password

set_env DJANGO_SECRET_KEY \
  ci-production-secret-key-0123456789-abcdefghijklmnopqrstuvwxyz
set_env DJANGO_DEBUG False
set_env ALLOWED_HOSTS dodugir.com,www.dodugir.com
set_env CORS_ALLOWED_ORIGINS https://dodugir.com
set_env CSRF_TRUSTED_ORIGINS https://dodugir.com
set_env SITE_URL https://dodugir.com
set_env FRONTEND_URL https://dodugir.com
set_env BILLING_RETURN_URL_ALLOWED_ORIGINS https://dodugir.com
set_env POSTGRES_DB "$POSTGRES_DB"
set_env POSTGRES_USER "$POSTGRES_USER"
set_env POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
set_env DATABASE_URL \
  "postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}"
set_env CACHE_REDIS_PASSWORD "$CACHE_REDIS_PASSWORD"
set_env CELERY_REDIS_PASSWORD "$CELERY_REDIS_PASSWORD"
set_env CACHE_REDIS_URL \
  "redis://:${CACHE_REDIS_PASSWORD}@redis:6379/0"
set_env CELERY_BROKER_URL \
  "redis://:${CELERY_REDIS_PASSWORD}@redis_broker:6379/0"
set_env CELERY_RESULT_BACKEND \
  "redis://:${CELERY_REDIS_PASSWORD}@redis_broker:6379/1"
set_env COORDINATION_REDIS_URL \
  "redis://:${CELERY_REDIS_PASSWORD}@redis_broker:6379/2"
set_env YC_S3_BUCKET ci-production-bucket
set_env YC_S3_ACCESS_KEY ci-production-access
set_env YC_S3_SECRET_KEY ci-production-secret
set_env FIELD_ENCRYPTION_KEY \
  Y2ktb25seS1mZXJuZXQta2V5LTMyLWJ5dGVzISEhISE=
set_env FIELD_ENCRYPTION_KEYS \
  Y2ktb25seS1mZXJuZXQta2V5LTMyLWJ5dGVzISEhISE=
set_env BILLING_ENABLED false
set_env YOOKASSA_SHOP_ID ''
set_env YOOKASSA_SECRET_KEY ''
set_env YOOKASSA_ALLOW_TEST_PAYMENTS false
set_env RESEND_API_KEY re_ci_runtime_smoke_key_1234567890
set_env DEFAULT_FROM_EMAIL noreply@notify.dodugir.com

mkdir -p "$CI_CERTIFICATE_LIVE_DIR" "$CI_CERTIFICATE_ARCHIVE_DIR"
openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
  -subj '/CN=dodugir.com' \
  -keyout "$CI_CERTIFICATE_LIVE_DIR/privkey.pem" \
  -out "$CI_CERTIFICATE_LIVE_DIR/fullchain.pem" \
  >/dev/null 2>&1
chmod 600 "$CI_CERTIFICATE_LIVE_DIR/privkey.pem"
chmod 644 "$CI_CERTIFICATE_LIVE_DIR/fullchain.pem"

"${COMPOSE[@]}" up -d db redis redis_broker egress_proxy django frontend nginx
for service in \
  db redis redis_broker egress_proxy django frontend nginx
do
  wait_healthy "$service"
done

nginx_id="$("${COMPOSE[@]}" ps -q nginx)"
docker port "$nginx_id" 80/tcp | grep -Eq '(^|:)80$'
docker port "$nginx_id" 443/tcp | grep -Eq '(^|:)443$'
curl --fail --insecure --silent --show-error --max-time 15 --noproxy '*' \
  --resolve dodugir.com:443:127.0.0.1 \
  https://dodugir.com/api/v1/live/ \
  | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"'
curl --fail --insecure --silent --show-error --max-time 15 --noproxy '*' \
  --resolve dodugir.com:443:127.0.0.1 \
  https://dodugir.com/api/v1/ready/ \
  | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ready"'
redirect="$(
  curl --silent --show-error --max-time 15 --noproxy '*' \
    --output /dev/null --write-out '%{http_code} %{redirect_url}' \
    --resolve dodugir.com:80:127.0.0.1 \
    http://dodugir.com/
)"
[[ "$redirect" == '301 https://dodugir.com/' ]]

"${COMPOSE[@]}" exec -T django \
  python manage.py check_public_http_connectivity
"${COMPOSE[@]}" exec -T django \
  python scripts/ci_verify_egress_proxy.py

echo 'Production runtime boot, ingress publish and egress controls: ok'
