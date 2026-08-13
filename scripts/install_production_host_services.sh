#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT_DIR="/opt/saas_poster"
DEPLOY_PUBLIC_KEY_FILE="${1:-}"
DEFER_TIMERS="${SAAS_POSTER_DEFER_TIMERS:-false}"

fail() {
  echo "host service installation failed: $*" >&2
  exit 1
}

validate_install_source() {
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
  [[ -z "$unsafe_path" ]] \
    || fail "unsafe checkout path: $unsafe_path"
}

[[ "$EUID" -eq 0 ]] || fail "must run as root"
[[ "$DEFER_TIMERS" == "true" || "$DEFER_TIMERS" == "false" ]] \
  || fail "SAAS_POSTER_DEFER_TIMERS must be true or false"
[[ "$(pwd -P)" == "$ROOT_DIR" ]] || fail "run from $ROOT_DIR"
validate_install_source
[[ -f "$DEPLOY_PUBLIC_KEY_FILE" && ! -L "$DEPLOY_PUBLIC_KEY_FILE" ]] \
  || fail "pass a regular deploy public-key file"
ssh-keygen -l -f "$DEPLOY_PUBLIC_KEY_FILE" >/dev/null \
  || fail "invalid SSH public key"

if ! id mapdeploy >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash mapdeploy
fi
mapdeploy_uid="$(id -u mapdeploy)"
mapdeploy_primary_group="$(id -gn mapdeploy)"
mapdeploy_groups="$(id -nG mapdeploy)"
mapdeploy_home="$(getent passwd mapdeploy | cut -d: -f6)"
[[ "$mapdeploy_uid" =~ ^[1-9][0-9]*$ ]] \
  || fail "mapdeploy must be an unprivileged account"
[[ "$mapdeploy_primary_group" == "mapdeploy" && "$mapdeploy_groups" == "mapdeploy" ]] \
  || fail "mapdeploy has unexpected supplementary or privileged groups"
[[ "$mapdeploy_home" == "/home/mapdeploy" ]] \
  || fail "mapdeploy has an unexpected home directory"
# Keep the account usable for public-key SSH while making password knowledge
# impossible. A dedicated sshd Match block below enforces publickey-only auth.
command -v openssl >/dev/null 2>&1 || fail "openssl is required"
random_password_hash="$(openssl passwd -6 "$(openssl rand -hex 32)")"
usermod --password "$random_password_hash" --shell /bin/bash mapdeploy

install -o root -g root -m 0755 \
  scripts/validate_production_checkout.sh \
  /usr/local/sbin/saas-poster-validate-checkout
install -o root -g root -m 0755 \
  scripts/production_release.sh /usr/local/sbin/saas-poster-release
install -o root -g root -m 0755 \
  scripts/production_deploy_gateway.sh \
  /usr/local/sbin/saas-poster-deploy-gateway
install -o root -g root -m 0755 \
  scripts/verify_production_topology.sh \
  /usr/local/sbin/saas-poster-verify-topology
install -o root -g root -m 0755 \
  scripts/check_production_capacity.sh \
  /usr/local/sbin/saas-poster-check-capacity
install -o root -g root -m 0755 \
  scripts/production_backup.sh /usr/local/sbin/saas-poster-backup
install -o root -g root -m 0755 \
  scripts/production_backup_check.sh \
  /usr/local/sbin/saas-poster-backup-check
install -o root -g root -m 0755 \
  scripts/reload_production_nginx.sh \
  /usr/local/sbin/saas-poster-reload-nginx
install -o root -g root -m 0755 \
  scripts/rotate_backup_db_password.sh \
  /usr/local/sbin/saas-poster-rotate-backup-db-password

install -d -o mapdeploy -g mapdeploy -m 0700 /home/mapdeploy/.ssh
deploy_public_key="$(<"$DEPLOY_PUBLIC_KEY_FILE")"
[[ "$deploy_public_key" != *$'\n'* ]] \
  || fail "deploy public-key file must contain exactly one key"
case "$deploy_public_key" in
  ssh-ed25519\ *) ;;
  *) fail "only an Ed25519 deploy key is accepted" ;;
esac
authorized_keys_tmp="$(mktemp)"
trap 'rm -f -- "$authorized_keys_tmp"' EXIT
printf '%s %s\n' \
  'restrict,command="/usr/local/sbin/saas-poster-deploy-gateway"' \
  "$deploy_public_key" >"$authorized_keys_tmp"
install -o mapdeploy -g mapdeploy -m 0600 "$authorized_keys_tmp" \
  /home/mapdeploy/.ssh/authorized_keys

install -d -o root -g root -m 0755 /etc/ssh/sshd_config.d
install -o root -g root -m 0644 ops/ssh/90-saas-poster-mapdeploy.conf \
  /etc/ssh/sshd_config.d/90-saas-poster-mapdeploy.conf
sshd -t
systemctl reload ssh.service 2>/dev/null || systemctl reload sshd.service

visudo -cf ops/sudoers/saas-poster-deploy >/dev/null
install -o root -g root -m 0440 ops/sudoers/saas-poster-deploy \
  /etc/sudoers.d/saas-poster-deploy

install -o root -g root -m 0644 ops/systemd/saas-poster-backup.service \
  ops/systemd/saas-poster-backup.timer \
  ops/systemd/saas-poster-backup-check.service \
  ops/systemd/saas-poster-backup-check.timer /etc/systemd/system/
install -o root -g root -m 0644 ops/tmpfiles/saas-poster.conf \
  /etc/tmpfiles.d/saas-poster.conf
systemd-tmpfiles --create /etc/tmpfiles.d/saas-poster.conf

install -d -o root -g root -m 0755 \
  /etc/letsencrypt/renewal-hooks/deploy
ln -sfn /usr/local/sbin/saas-poster-reload-nginx \
  /etc/letsencrypt/renewal-hooks/deploy/saas-poster-nginx-reload

systemctl daemon-reload
if [[ "$DEFER_TIMERS" == "true" ]]; then
  echo "backup timers remain disabled until the matching release succeeds"
else
  systemctl enable --now saas-poster-backup.timer \
    saas-poster-backup-check.timer
fi

echo "production host services installed"
