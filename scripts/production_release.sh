#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Root-only release entrypoint used by the restricted CI SSH account.  The
# caller can select only the exact current origin/main commit; arbitrary shell
# commands, branches and local commits are deliberately unsupported.
ROOT_DIR="/opt/saas_poster"
PROD_LOCK_DIR="/run/lock/saas-poster"
DEPLOY_LOCK_FILE="$PROD_LOCK_DIR/deploy.lock"
TARGET_SHA="${1:-}"
PREVIOUS_SHA=""
checkout_changed=false

restore_checkout_on_launcher_failure() {
  local exit_code="$?"
  trap - EXIT HUP INT TERM
  if [[ "$checkout_changed" == "true" && "$PREVIOUS_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    git -C "$ROOT_DIR" checkout --detach "$PREVIOUS_SHA" >/dev/null || {
      echo "CRITICAL: failed to restore production checkout to $PREVIOUS_SHA" >&2
      exit 70
    }
  fi
  exit "$exit_code"
}
trap restore_checkout_on_launcher_failure EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

fail() {
  echo "production release rejected: $*" >&2
  exit 1
}

[[ "$EUID" -eq 0 ]] || fail "must run as root"
[[ "$#" -eq 1 && "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] \
  || fail "expected exactly one full commit SHA"
/usr/local/sbin/saas-poster-validate-checkout >/dev/null \
  || fail "canonical checkout ownership or permissions are unsafe"
[[ -d "$ROOT_DIR/.git" && ! -L "$ROOT_DIR" ]] \
  || fail "canonical checkout is unavailable"
[[ -d "$PROD_LOCK_DIR" && ! -L "$PROD_LOCK_DIR" && -O "$PROD_LOCK_DIR" ]] \
  || fail "deploy lock directory is unavailable"
[[ "$(stat -c '%a' "$PROD_LOCK_DIR")" == "700" ]] \
  || fail "deploy lock directory must have mode 700"
exec 9>"$DEPLOY_LOCK_FILE"
flock -n 9 || fail "another release is already running"
export DEPLOY_LOCK_FD=9

cd "$ROOT_DIR"
PREVIOUS_SHA="$(git rev-parse --verify 'HEAD^{commit}')"
[[ "$PREVIOUS_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "invalid current release SHA"

git fetch --no-tags origin main
MAIN_SHA="$(git rev-parse --verify 'FETCH_HEAD^{commit}')"
if [[ "$TARGET_SHA" != "$MAIN_SHA" ]]; then
  echo "stale release skipped: origin/main is ${MAIN_SHA}"
  exit 0
fi

git cat-file -e "${TARGET_SHA}^{commit}"
git merge-base --is-ancestor "$PREVIOUS_SHA" "$TARGET_SHA" \
  || fail "current release is not an ancestor of target"
checkout_changed=true
git checkout --detach "$TARGET_SHA"

export PREVIOUS_SHA
exec "$ROOT_DIR/deploy.sh" "$TARGET_SHA"
