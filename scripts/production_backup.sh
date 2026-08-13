#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="/opt/saas_poster"
PROD_LOCK_DIR="/run/lock/saas-poster"
DEPLOY_LOCK_FILE="$PROD_LOCK_DIR/deploy.lock"

fail() {
  echo "production backup rejected: $*" >&2
  exit 1
}

[[ "$EUID" -eq 0 ]] || fail "must run as root"
/usr/local/sbin/saas-poster-validate-checkout >/dev/null \
  || fail "canonical checkout ownership or permissions are unsafe"
[[ -d "$ROOT_DIR/.git" && ! -L "$ROOT_DIR" ]] \
  || fail "canonical checkout is unavailable"
[[ -d "$PROD_LOCK_DIR" && ! -L "$PROD_LOCK_DIR" && -O "$PROD_LOCK_DIR" ]] \
  || fail "deploy lock directory is unavailable"
[[ "$(stat -c '%a' "$PROD_LOCK_DIR")" == "700" ]] \
  || fail "deploy lock directory must have mode 700"
exec 9>"$DEPLOY_LOCK_FILE"
flock 9

cd "$ROOT_DIR"

DEPLOY_GIT_SHA="$(git rev-parse --verify 'HEAD^{commit}')"
if [[ ! "$DEPLOY_GIT_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Cannot determine the deployed 40-character commit SHA" >&2
  exit 1
fi
export DEPLOY_GIT_SHA

COMPOSE=(
  docker compose
  --project-name saas_poster
  --project-directory "$ROOT_DIR"
  -f "$ROOT_DIR/docker-compose.prod.yml"
)
egress_container="$("${COMPOSE[@]}" ps -q egress_proxy)"
[[ -n "$egress_container" ]] || fail "egress proxy is not running"
[[ "$(docker inspect --format '{{.State.Health.Status}}' "$egress_container")" == "healthy" ]] \
  || fail "egress proxy is not healthy"

exec "${COMPOSE[@]}" --profile ops run --rm --no-deps backup
