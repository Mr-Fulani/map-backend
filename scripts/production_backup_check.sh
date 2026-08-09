#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE=(docker compose -f docker-compose.prod.yml)
"${COMPOSE[@]}" up -d --no-build egress_proxy
exec "${COMPOSE[@]}" --profile ops run --rm --no-deps \
  backup python3 -m backup.check_freshness
