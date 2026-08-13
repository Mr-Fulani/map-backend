#!/usr/bin/env bash
set -Eeuo pipefail

# ForcedCommand for the CI SSH key.  Never evaluate SSH_ORIGINAL_COMMAND: parse
# a tiny protocol and delegate only to reviewed, root-owned entrypoints.
original_command="${SSH_ORIGINAL_COMMAND:-}"
read -r -a command_parts <<<"$original_command"

case "${command_parts[0]:-}" in
  deploy)
    [[ "${#command_parts[@]}" -eq 2 ]] \
      || { echo "usage: deploy <40-char-sha>" >&2; exit 64; }
    [[ "${command_parts[1]}" =~ ^[0-9a-f]{40}$ ]] \
      || { echo "invalid deploy SHA" >&2; exit 64; }
    exec sudo -n /usr/local/sbin/saas-poster-release "${command_parts[1]}"
    ;;
  backup-check)
    [[ "${#command_parts[@]}" -eq 1 ]] \
      || { echo "usage: backup-check" >&2; exit 64; }
    exec sudo -n systemctl start --wait saas-poster-backup-check.service
    ;;
  topology-check)
    [[ "${#command_parts[@]}" -eq 1 ]] \
      || { echo "usage: topology-check" >&2; exit 64; }
    exec sudo -n /usr/local/sbin/saas-poster-verify-topology
    ;;
  capacity-check)
    [[ "${#command_parts[@]}" -eq 1 ]] \
      || { echo "usage: capacity-check" >&2; exit 64; }
    exec sudo -n /usr/local/sbin/saas-poster-check-capacity
    ;;
  *)
    echo "command is not allowed" >&2
    exit 64
    ;;
esac
