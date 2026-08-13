#!/usr/bin/env bash
set -Eeuo pipefail

# This helper is installed as a root-owned host entrypoint.  Root services may
# read Compose, Git and application files from the canonical checkout only
# after the whole tree has been proven immutable to unprivileged accounts.
ROOT_DIR="/opt/saas_poster"

fail() {
  echo "production checkout validation failed: $*" >&2
  exit 1
}

[[ "$EUID" -eq 0 ]] || fail "must run as root"
[[ "$(readlink -f -- "$ROOT_DIR")" == "$ROOT_DIR" ]] \
  || fail "canonical checkout path is missing or resolves through a symlink"
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
[[ -z "$unsafe_path" ]] \
  || fail "checkout contains a symlink, non-root owner, or writable path: $unsafe_path"

echo "production checkout ownership and permissions are safe"
