#!/usr/bin/env bash
set -Eeuo pipefail

# Verify the effective, running Docker topology. Compose configuration alone is
# not sufficient: an existing container can retain a stale network attachment
# from an older release and silently regain unrestricted egress.
ROOT_DIR="/opt/saas_poster"
if [[ "${CI:-}" == "true" ]]; then
  ROOT_DIR="${PRODUCTION_ROOT:-$PWD}"
else
  /usr/local/sbin/saas-poster-validate-checkout >/dev/null
fi

COMPOSE=(
  docker compose
  --project-name saas_poster
  --project-directory "$ROOT_DIR"
  -f "$ROOT_DIR/docker-compose.prod.yml"
)

fail() {
  echo "production topology check failed: $*" >&2
  exit 1
}

expected_networks() {
  case "$1" in
    db|redis|redis_broker|django|celery_worker|celery_beat|celery_worker_images|frontend)
      printf '%s\n' saas_poster_backend
      ;;
    egress_proxy)
      printf '%s\n' saas_poster_backend saas_poster_egress_public
      ;;
    nginx)
      printf '%s\n' saas_poster_backend saas_poster_ingress_public
      ;;
    *)
      fail "unknown service $1"
      ;;
  esac
}

verify_service() {
  local service="$1"
  local container_id state health actual expected

  container_id="$("${COMPOSE[@]}" ps -q "$service")"
  [[ -n "$container_id" ]] || fail "$service has no running container"

  state="$(docker inspect --format '{{.State.Status}}' "$container_id")"
  health="$(
    docker inspect \
      --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
      "$container_id"
  )"
  [[ "$state" == "running" ]] || fail "$service state is $state"
  [[ "$health" == "healthy" || "$health" == "none" ]] \
    || fail "$service health is $health"

  actual="$(
    docker inspect \
      --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
      "$container_id" | sed '/^$/d' | LC_ALL=C sort
  )"
  expected="$(expected_networks "$service" | LC_ALL=C sort)"
  [[ "$actual" == "$expected" ]] || {
    echo "service=$service" >&2
    echo "expected networks:" >&2
    printf '%s\n' "$expected" >&2
    echo "actual networks:" >&2
    printf '%s\n' "$actual" >&2
    fail "$service has unexpected Docker network membership"
  }
}

for service in \
  db redis redis_broker egress_proxy django celery_worker celery_beat \
  celery_worker_images frontend nginx
do
  verify_service "$service"
done

nginx_id="$("${COMPOSE[@]}" ps -q nginx)"
published_ports="$(docker port "$nginx_id")"
grep -Eq '^80/tcp -> .*:80$' <<<"$published_ports" \
  || fail "nginx does not publish TCP 80"
grep -Eq '^443/tcp -> .*:443$' <<<"$published_ports" \
  || fail "nginx does not publish TCP 443"

echo "production topology is healthy and exact"
