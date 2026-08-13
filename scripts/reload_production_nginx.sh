#!/usr/bin/env bash
set -Eeuo pipefail

# Certbot executes deploy hooks as a privileged service. Keep the checkout path
# constant so an inherited environment variable cannot redirect Compose to an
# attacker-controlled file. Changing the production layout requires a reviewed
# script change together with the hook path.
ROOT_DIR="/opt/saas_poster"
/usr/local/sbin/saas-poster-validate-checkout >/dev/null || {
  echo "Production checkout ownership or permissions are unsafe" >&2
  exit 1
}
[[ -f "$ROOT_DIR/docker-compose.prod.yml" ]] || {
  echo "Production Compose file is missing under $ROOT_DIR" >&2
  exit 1
}

COMPOSE=(
  docker compose
  --project-name saas_poster
  --project-directory "$ROOT_DIR"
  -f "$ROOT_DIR/docker-compose.prod.yml"
)

"${COMPOSE[@]}" exec -T nginx nginx -t
"${COMPOSE[@]}" exec -T nginx nginx -s reload
