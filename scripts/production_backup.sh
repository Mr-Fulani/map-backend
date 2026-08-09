#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
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
exec "${COMPOSE[@]}" --profile ops run --rm backup
