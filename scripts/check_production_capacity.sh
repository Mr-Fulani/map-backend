#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="/opt/saas_poster"
MIN_TOTAL_MEMORY_KB=3670016
MIN_AVAILABLE_MEMORY_KB=1048576
MIN_FREE_DISK_KB=8388608
MAX_LOAD_PER_CPU=2

fail() {
  echo "production capacity check failed: $*" >&2
  exit 1
}

if [[ "${CI:-}" != "true" ]]; then
  /usr/local/sbin/saas-poster-validate-checkout >/dev/null \
    || fail "canonical checkout ownership or permissions are unsafe"
fi

total_memory_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
available_memory_kb="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)"
free_disk_kb="$(df -Pk "$ROOT_DIR" | awk 'NR == 2 {print $4}')"
cpu_count="$(getconf _NPROCESSORS_ONLN)"
load_one="$(awk '{print $1}' /proc/loadavg)"

[[ "$total_memory_kb" =~ ^[0-9]+$ ]] || fail "cannot read total memory"
[[ "$available_memory_kb" =~ ^[0-9]+$ ]] \
  || fail "cannot read available memory"
[[ "$free_disk_kb" =~ ^[0-9]+$ ]] || fail "cannot read free disk"
[[ "$cpu_count" =~ ^[1-9][0-9]*$ ]] || fail "cannot read CPU count"
(( total_memory_kb >= MIN_TOTAL_MEMORY_KB )) \
  || fail "host memory is below the supported 3584 MiB production tier"
(( available_memory_kb >= MIN_AVAILABLE_MEMORY_KB )) \
  || fail "available memory is below 1024 MiB required for a sequential build"
(( free_disk_kb >= MIN_FREE_DISK_KB )) || fail "free disk is below 8 GiB"
awk \
  -v one_minute_load="$load_one" \
  -v cpu_total="$cpu_count" \
  -v max_per_cpu="$MAX_LOAD_PER_CPU" \
  'BEGIN {exit !(one_minute_load <= cpu_total * max_per_cpu)}' \
  || fail "one-minute load is above ${MAX_LOAD_PER_CPU}x CPU count"

while IFS= read -r container_id; do
  [[ -n "$container_id" ]] || continue
  read -r name oom_killed restart_count <<<"$(
    docker inspect --format \
      '{{.Name}} {{.State.OOMKilled}} {{.RestartCount}}' "$container_id"
  )"
  [[ "$oom_killed" == "false" ]] || fail "$name was OOM-killed"
  [[ "$restart_count" == "0" ]] || fail "$name restart count is $restart_count"
done < <(
  docker ps --quiet --filter 'label=com.docker.compose.project=saas_poster'
)

echo "production capacity is within safety thresholds"
