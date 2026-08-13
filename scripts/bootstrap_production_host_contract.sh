#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Out-of-band root bootstrap for reviewed host-contract changes.  The normal
# forced-command deploy cannot update its own already-installed launcher before
# that launcher runs.  This script temporarily checks out the exact successful
# origin/main target under the shared release lock, installs the contract, and
# restores the actually running release before normal deployment starts.
ROOT_DIR="/opt/saas_poster"
PROD_LOCK_DIR="/run/lock/saas-poster"
DEPLOY_LOCK_FILE="$PROD_LOCK_DIR/deploy.lock"
TARGET_SHA="${1:-}"
DEPLOY_PUBLIC_KEY_FILE="${2:-}"
HOST_CONTRACT_PENDING_FILE="$PROD_LOCK_DIR/host-contract-pending"
PREVIOUS_SHA=""
checkout_changed=false
backup_timers_quiesced=false

fail() {
  echo "production host bootstrap rejected: $*" >&2
  exit 1
}

validate_bootstrap_source() {
  local path mode unsafe_path
  [[ "$(readlink -f -- "$ROOT_DIR")" == "$ROOT_DIR" ]] \
    || fail "canonical checkout resolves through a symlink"
  [[ -d "$ROOT_DIR/.git" && ! -L "$ROOT_DIR/.git" ]] \
    || fail "canonical Git checkout is unavailable"
  for path in / /opt "$ROOT_DIR" "$ROOT_DIR/.git"; do
    [[ ! -L "$path" && "$(stat -c '%u' -- "$path")" == "0" ]] \
      || fail "$path must be a root-owned non-symlink"
    mode="$(stat -c '%a' -- "$path")"
    (( (8#$mode & 8#022) == 0 )) \
      || fail "$path must not be group/world writable"
  done
  unsafe_path="$({
    find "$ROOT_DIR" -xdev \
      \( -type l -o ! -user root -o -perm /022 \) -print -quit
  } 2>/dev/null)" || fail "cannot inspect the complete production checkout"
  [[ -z "$unsafe_path" ]] || fail "unsafe checkout path: $unsafe_path"
}

restore_checkout() {
  local exit_code="$?"
  trap - EXIT HUP INT TERM
  if [[ "$checkout_changed" == "true" && "$PREVIOUS_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    git -C "$ROOT_DIR" checkout --detach "$PREVIOUS_SHA" >/dev/null || {
      echo "CRITICAL: failed to restore production checkout to $PREVIOUS_SHA" >&2
      exit 70
    }
  fi
  if [[ "$backup_timers_quiesced" == "true" ]]; then
    echo "CRITICAL: backup timers remain disabled until target deploy succeeds." >&2
  fi
  exit "$exit_code"
}
trap restore_checkout EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

[[ "$EUID" -eq 0 ]] || fail "must run as root"
[[ "$#" -eq 2 && "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] \
  || fail "expected target SHA and deploy public-key file"
validate_bootstrap_source
[[ -d "$ROOT_DIR/.git" && ! -L "$ROOT_DIR" ]] \
  || fail "canonical checkout is unavailable"
[[ -f "$DEPLOY_PUBLIC_KEY_FILE" && ! -L "$DEPLOY_PUBLIC_KEY_FILE" \
  && -O "$DEPLOY_PUBLIC_KEY_FILE" ]] \
  || fail "deploy public key must be a root-owned regular file"
[[ -d "$PROD_LOCK_DIR" && ! -L "$PROD_LOCK_DIR" && -O "$PROD_LOCK_DIR" ]] \
  || fail "deploy lock directory is unavailable"
[[ "$(stat -c '%a' "$PROD_LOCK_DIR")" == "700" ]] \
  || fail "deploy lock directory must have mode 700"

exec 9>"$DEPLOY_LOCK_FILE"
flock -n 9 || fail "another release is already running"

# Legacy backup wrappers did not share this lock. Stop their timers and wait
# for any already-running oneshot to finish naturally before checkout changes.
for timer_unit in saas-poster-backup.timer saas-poster-backup-check.timer; do
  if [[ "$(systemctl show -p LoadState --value "$timer_unit" 2>/dev/null || true)" \
    != "not-found" ]]; then
    systemctl disable --now "$timer_unit"
  fi
done
backup_timers_quiesced=true
backup_wait_deadline=$((SECONDS + 10800))
while systemctl is-active --quiet saas-poster-backup.service \
  || systemctl is-active --quiet saas-poster-backup-check.service; do
  (( SECONDS < backup_wait_deadline )) \
    || fail "timed out waiting for running backup services"
  sleep 2
done

cd "$ROOT_DIR"
PREVIOUS_SHA="$(git rev-parse --verify 'HEAD^{commit}')"
[[ "$PREVIOUS_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "invalid current release SHA"
[[ -z "$(git status --porcelain=v1 --untracked-files=normal --ignore-submodules=none)" ]] \
  || fail "production checkout contains drift"

git fetch --no-tags origin main
MAIN_SHA="$(git rev-parse --verify 'FETCH_HEAD^{commit}')"
[[ "$TARGET_SHA" == "$MAIN_SHA" ]] || fail "target is not exact current origin/main"
git merge-base --is-ancestor "$PREVIOUS_SHA" "$TARGET_SHA" \
  || fail "current release is not an ancestor of target"

checkout_changed=true
git checkout --detach "$TARGET_SHA"
[[ -z "$(git status --porcelain=v1 --untracked-files=normal --ignore-submodules=none)" ]] \
  || fail "target checkout contains drift"
SAAS_POSTER_DEFER_TIMERS=true \
  ./scripts/install_production_host_services.sh "$DEPLOY_PUBLIC_KEY_FILE"
cmp -s scripts/production_release.sh /usr/local/sbin/saas-poster-release \
  || fail "installed release entrypoint verification failed"
cmp -s scripts/production_deploy_gateway.sh \
  /usr/local/sbin/saas-poster-deploy-gateway \
  || fail "installed deploy gateway verification failed"
for installed_contract in \
  'scripts/validate_production_checkout.sh:/usr/local/sbin/saas-poster-validate-checkout' \
  'scripts/verify_production_topology.sh:/usr/local/sbin/saas-poster-verify-topology' \
  'scripts/check_production_capacity.sh:/usr/local/sbin/saas-poster-check-capacity' \
  'scripts/production_backup.sh:/usr/local/sbin/saas-poster-backup' \
  'scripts/production_backup_check.sh:/usr/local/sbin/saas-poster-backup-check' \
  'scripts/reload_production_nginx.sh:/usr/local/sbin/saas-poster-reload-nginx' \
  'scripts/rotate_backup_db_password.sh:/usr/local/sbin/saas-poster-rotate-backup-db-password'
do
  source_path="${installed_contract%%:*}"
  installed_path="${installed_contract#*:}"
  cmp -s "$source_path" "$installed_path" \
    || fail "installed host contract verification failed: $installed_path"
done

pending_tmp="$(mktemp "$PROD_LOCK_DIR/.host-contract-pending.XXXXXX")"
printf '%s\n' "$TARGET_SHA" >"$pending_tmp"
chown root:root "$pending_tmp"
chmod 0600 "$pending_tmp"
mv -f -- "$pending_tmp" "$HOST_CONTRACT_PENDING_FILE"

git checkout --detach "$PREVIOUS_SHA"
checkout_changed=false
[[ "$(git rev-parse --verify 'HEAD^{commit}')" == "$PREVIOUS_SHA" ]] \
  || fail "failed to restore current release checkout"
[[ -z "$(git status --porcelain=v1 --untracked-files=normal --ignore-submodules=none)" ]] \
  || fail "restored checkout contains drift"
trap - EXIT HUP INT TERM
backup_timers_quiesced=false
echo "production host contract installed; checkout restored to $PREVIOUS_SHA"
