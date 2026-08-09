#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RESTORE_ENV_FILE="${RESTORE_ENV_FILE:-/secure/saas-poster/restore.env}"
RESTORE_PROJECT_NAME="saas-poster-restore"
RESTORE_LOCK_DIR="/run/lock/saas-poster"
RESTORE_LOCK_FILE="$RESTORE_LOCK_DIR/restore.lock"
COMPOSE=(
  docker compose
  --project-name "$RESTORE_PROJECT_NAME"
  --project-directory "$ROOT_DIR"
  --env-file "$RESTORE_ENV_FILE"
  -f "$ROOT_DIR/docker-compose.restore.yml"
)

fail() {
  echo "Restore preflight failed: $*" >&2
  exit 1
}

file_mode() {
  stat -c '%a' "$1"
}

cleanup() {
  local exit_code="$1"
  local cleanup_code
  trap - EXIT HUP INT TERM
  set +e
  "${COMPOSE[@]}" down --volumes --remove-orphans
  cleanup_code="$?"
  if ((cleanup_code != 0)); then
    echo "CRITICAL: restore cleanup failed; the plaintext dump may remain in" \
      "Compose project ${RESTORE_PROJECT_NAME}. Run the documented down" \
      "--volumes command before any further restore." >&2
    if ((exit_code == 0)); then
      exit 1
    fi
  fi
  exit "$exit_code"
}

for command_name in docker flock stat; do
  command -v "$command_name" >/dev/null 2>&1 \
    || fail "required command is not installed: $command_name"
done

[[ -f "$RESTORE_ENV_FILE" && ! -L "$RESTORE_ENV_FILE" ]] \
  || fail "RESTORE_ENV_FILE must be a regular non-symlink file: $RESTORE_ENV_FILE"
case "$(file_mode "$RESTORE_ENV_FILE")" in
  400|600) ;;
  *) fail "RESTORE_ENV_FILE must have mode 400 or 600" ;;
esac

[[ -d "$RESTORE_LOCK_DIR" && ! -L "$RESTORE_LOCK_DIR" && -O "$RESTORE_LOCK_DIR" ]] \
  || fail "$RESTORE_LOCK_DIR must be a non-symlink directory owned by the restore user"
[[ "$(file_mode "$RESTORE_LOCK_DIR")" == "700" ]] \
  || fail "$RESTORE_LOCK_DIR must have mode 700"

# One host-wide lock protects the fixed Compose project and its plaintext
# workspace across every RESTORE_ENV_FILE. Locking the env file itself would let
# two different drill configs delete one another's containers/volume.
exec 9>"$RESTORE_LOCK_FILE"
flock -n 9 || fail "another restore process is already running on this host"

# A restore file is trusted operator input. Loading it keeps secret values out of
# process arguments; the dedicated Compose file forwards only the allowlisted
# RESTORE_* variables into the container.
set -a
# shellcheck disable=SC1090
source "$RESTORE_ENV_FILE"
set +a

required_variables=(
  RESTORE_PRODUCTION_DATABASE_NAME
  RESTORE_DATABASE_URL
  RESTORE_CONFIRM_DATABASE
  RESTORE_OBJECT_KEY
  RESTORE_AGE_IDENTITY_HOST_FILE
  RESTORE_SIGNING_PUBLIC_KEY
  RESTORE_S3_BUCKET
  RESTORE_S3_ENDPOINT
  RESTORE_S3_ACCESS_KEY
  RESTORE_S3_SECRET_KEY
)
for variable_name in "${required_variables[@]}"; do
  [[ -n "${!variable_name:-}" ]] || fail "$variable_name is required"
done

[[ "$RESTORE_AGE_IDENTITY_HOST_FILE" == /* ]] \
  || fail "RESTORE_AGE_IDENTITY_HOST_FILE must be an absolute path"
[[ -f "$RESTORE_AGE_IDENTITY_HOST_FILE" && ! -L "$RESTORE_AGE_IDENTITY_HOST_FILE" ]] \
  || fail "age identity must be a regular non-symlink file"
case "$(file_mode "$RESTORE_AGE_IDENTITY_HOST_FILE")" in
  400|600) ;;
  *) fail "age identity must have mode 400 or 600" ;;
esac

for forbidden_variable in \
  BACKUP_DATABASE_URL \
  BACKUP_S3_ACCESS_KEY \
  BACKUP_S3_SECRET_KEY \
  BACKUP_SIGNING_PRIVATE_KEY \
  BACKUP_AGE_RECIPIENTS; do
  [[ -z "${!forbidden_variable:-}" ]] \
    || fail "$forbidden_variable must not be present in the restore environment"
done

trap 'cleanup $?' EXIT
trap 'cleanup 129' HUP
trap 'cleanup 130' INT
trap 'cleanup 143' TERM
# Remove resources left by SIGKILL/host failure before a new archive can enter
# the fixed plaintext workspace. The global lock guarantees no live run owns it.
"${COMPOSE[@]}" down --volumes --remove-orphans
"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" build restore
"${COMPOSE[@]}" run --rm restore
