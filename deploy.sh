#!/usr/bin/env bash
# Production deploy for one immutable commit that has already passed CI.
set -Eeuo pipefail
export COMPOSE_PARALLEL_LIMIT=1

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"
PROD_LOCK_DIR="/run/lock/saas-poster"
DEPLOY_LOCK_FILE="$PROD_LOCK_DIR/deploy.lock"
HOST_CONTRACT_PENDING_FILE="$PROD_LOCK_DIR/host-contract-pending"

fail() {
  echo "ОШИБКА: $*" >&2
  return 1
}

TARGET_SHA="${1:-}"
PREVIOUS_SHA="${PREVIOUS_SHA:-}"

# The release launcher has already checked out TARGET_SHA. Keep this small
# fail-safe active while lock and config are validated; the runtime-aware trap
# below replaces it before any Docker mutation.
restore_checkout_before_runtime() {
  local exit_code="$1"
  trap - ERR HUP INT TERM
  set +e
  if [[ "$PREVIOUS_SHA" =~ ^[0-9a-f]{40}$ \
    && "$TARGET_SHA" =~ ^[0-9a-f]{40}$ \
    && "$PREVIOUS_SHA" != "$TARGET_SHA" ]] \
    && git cat-file -e "${PREVIOUS_SHA}^{commit}" 2>/dev/null; then
    git checkout --detach "$PREVIOUS_SHA" >/dev/null || \
      echo "КРИТИЧЕСКАЯ ОШИБКА: checkout $PREVIOUS_SHA не восстановлен." >&2
  fi
  exit "$exit_code"
}
trap 'restore_checkout_before_runtime $?' ERR
trap 'restore_checkout_before_runtime 129' HUP
trap 'restore_checkout_before_runtime 130' INT
trap 'restore_checkout_before_runtime 143' TERM

[[ "$EUID" -eq 0 ]] || fail "production deploy должен выполняться как root."
[[ "$ROOT_DIR" == "/opt/saas_poster" ]] \
  || fail "production deploy разрешён только из canonical checkout."
/usr/local/sbin/saas-poster-validate-checkout >/dev/null \
  || fail "canonical checkout имеет небезопасного владельца или права."
[[ -d "$PROD_LOCK_DIR" && ! -L "$PROD_LOCK_DIR" && -O "$PROD_LOCK_DIR" ]] \
  || fail "$PROD_LOCK_DIR должен быть обычным каталогом владельца deploy user."
[[ "$(stat -c '%a' "$PROD_LOCK_DIR")" == "700" ]] \
  || fail "$PROD_LOCK_DIR должен иметь права 700."
if [[ "${DEPLOY_LOCK_FD:-}" == "9" ]]; then
  [[ "$(readlink -f "/proc/$$/fd/9" 2>/dev/null || true)" == "$DEPLOY_LOCK_FILE" ]] \
    || fail "унаследованный deploy lock недействителен."
else
  exec 9>"$DEPLOY_LOCK_FILE"
fi
flock -n 9 || fail "другой deploy уже выполняется."

validate_private_regular_file() {
  local secret_file="$1"
  local secret_mode

  [[ -e "$secret_file" || -L "$secret_file" ]] \
    || fail "отсутствует защищённый файл ${secret_file}."
  [[ -f "$secret_file" && ! -L "$secret_file" && -O "$secret_file" ]] \
    || fail "${secret_file} должен быть обычным файлом владельца deploy user, а не symlink."
  secret_mode="$(stat -c '%a' -- "$secret_file")" \
    || fail "не удалось проверить права ${secret_file}."
  [[ "$secret_mode" == "600" || "$secret_mode" == "400" ]] \
    || fail "${secret_file} должен иметь права 600 или 400 (сейчас ${secret_mode})."
}

load_deploy_env() {
  local deploy_env_file="$1"
  local line key value
  local line_number=0
  local -A seen_keys=()

  while IFS= read -r line || [[ -n "$line" ]]; do
    line_number=$((line_number + 1))
    if [[ "$line" =~ ^[[:space:]]*$ || "$line" =~ ^[[:space:]]*# ]]; then
      continue
    fi
    if [[ ! "$line" =~ ^([A-Z][A-Z0-9_]*)=([^[:space:]]+)$ ]]; then
      fail "${deploy_env_file}:${line_number}: ожидается простая запись KEY=value без пробелов и shell-синтаксиса."
      return 1
    fi

    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    if [[ "$value" == *'$'* || "$value" == *'`'* || "$value" == *\\* \
      || "$value" == *'"'* || "$value" == *"'"* ]]; then
      fail "${deploy_env_file}:${line_number}: кавычки, escaping и shell-подстановки в значениях запрещены."
      return 1
    fi
    case "$key" in
      PROD_SMOKE_URL | \
        PROD_MIN_FREE_DISK_MB | \
        PROD_HEALTH_RETRIES | \
        PROD_HEALTH_INTERVAL_SECONDS | \
        PROD_LOG_TAIL | \
        PROD_ROLLBACK_ENABLED | \
        PROD_BACKUP_TIMEOUT_SECONDS | \
        PROD_DRAIN_TIMEOUT_SECONDS | \
        PROD_BROKER_MIGRATION_CONFIRMED) ;;
      *)
        fail "${deploy_env_file}:${line_number}: параметр ${key} не разрешён в .deploy.env."
        return 1
        ;;
    esac
    if [[ -n "${seen_keys[$key]+x}" ]]; then
      fail "${deploy_env_file}:${line_number}: параметр ${key} указан повторно."
      return 1
    fi
    seen_keys["$key"]=1
    printf -v "$key" '%s' "$value"
    export "${key?}"
  done < "$deploy_env_file"
}

verify_installed_host_contract() {
  local pair source_file target_file expected_mode target_mode
  local pairs=(
    'scripts/validate_production_checkout.sh:/usr/local/sbin/saas-poster-validate-checkout:755'
    'scripts/production_release.sh:/usr/local/sbin/saas-poster-release:755'
    'scripts/production_deploy_gateway.sh:/usr/local/sbin/saas-poster-deploy-gateway:755'
    'scripts/verify_production_topology.sh:/usr/local/sbin/saas-poster-verify-topology:755'
    'scripts/check_production_capacity.sh:/usr/local/sbin/saas-poster-check-capacity:755'
    'scripts/production_backup.sh:/usr/local/sbin/saas-poster-backup:755'
    'scripts/production_backup_check.sh:/usr/local/sbin/saas-poster-backup-check:755'
    'scripts/reload_production_nginx.sh:/usr/local/sbin/saas-poster-reload-nginx:755'
    'scripts/rotate_backup_db_password.sh:/usr/local/sbin/saas-poster-rotate-backup-db-password:755'
    'ops/ssh/90-saas-poster-mapdeploy.conf:/etc/ssh/sshd_config.d/90-saas-poster-mapdeploy.conf:644'
    'ops/sudoers/saas-poster-deploy:/etc/sudoers.d/saas-poster-deploy:440'
    'ops/systemd/saas-poster-backup.service:/etc/systemd/system/saas-poster-backup.service:644'
    'ops/systemd/saas-poster-backup.timer:/etc/systemd/system/saas-poster-backup.timer:644'
    'ops/systemd/saas-poster-backup-check.service:/etc/systemd/system/saas-poster-backup-check.service:644'
    'ops/systemd/saas-poster-backup-check.timer:/etc/systemd/system/saas-poster-backup-check.timer:644'
    'ops/tmpfiles/saas-poster.conf:/etc/tmpfiles.d/saas-poster.conf:644'
  )

  for pair in "${pairs[@]}"; do
    IFS=: read -r source_file target_file expected_mode <<<"$pair"
    [[ -f "$target_file" && ! -L "$target_file" && -O "$target_file" ]] \
      || fail "host contract file ${target_file} не установлен безопасно."
    target_mode="$(stat -c '%a' "$target_file")" \
      || fail "не удалось проверить ${target_file}."
    [[ "$target_mode" == "$expected_mode" ]] \
      || fail "host contract file ${target_file} имеет небезопасный mode ${target_mode}."
    cmp -s "$source_file" "$target_file" \
      || fail "host contract file ${target_file} не совпадает с TARGET_SHA; выполните bootstrap."
  done
  [[ -L /etc/letsencrypt/renewal-hooks/deploy/saas-poster-nginx-reload ]] \
    || fail "Certbot nginx reload hook не установлен."
  [[ "$(readlink -f /etc/letsencrypt/renewal-hooks/deploy/saas-poster-nginx-reload)" \
    == "/usr/local/sbin/saas-poster-reload-nginx" ]] \
    || fail "Certbot nginx reload hook указывает на неверный target."
}

validate_pending_host_contract() {
  local pending_sha
  [[ -e "$HOST_CONTRACT_PENDING_FILE" || -L "$HOST_CONTRACT_PENDING_FILE" ]] \
    || return 0
  [[ -f "$HOST_CONTRACT_PENDING_FILE" && ! -L "$HOST_CONTRACT_PENDING_FILE" \
    && -O "$HOST_CONTRACT_PENDING_FILE" ]] \
    || fail "host-contract pending marker небезопасен."
  [[ "$(stat -c '%a' "$HOST_CONTRACT_PENDING_FILE")" == "600" ]] \
    || fail "host-contract pending marker должен иметь mode 600."
  pending_sha="$(<"$HOST_CONTRACT_PENDING_FILE")"
  [[ "$pending_sha" == "$TARGET_SHA" ]] \
    || fail "host contract ожидает другой target SHA."
}

DEPLOY_ENV_FILE="${DEPLOY_ENV_FILE:-$ROOT_DIR/.deploy.env}"
if [[ -e "$DEPLOY_ENV_FILE" || -L "$DEPLOY_ENV_FILE" ]]; then
  command -v stat >/dev/null 2>&1 || fail "команда stat не установлена."
  validate_private_regular_file "$DEPLOY_ENV_FILE"
  load_deploy_env "$DEPLOY_ENV_FILE"
fi

COMPOSE_FILE="$ROOT_DIR/docker-compose.prod.yml"
COMPOSE=(
  docker compose
  --project-name saas_poster
  --project-directory "$ROOT_DIR"
  -f "$COMPOSE_FILE"
)
BUILD_SERVICES=(django celery_worker celery_beat celery_worker_images frontend backup)
ROLLBACK_SERVICES=(egress_proxy "${BUILD_SERVICES[@]}")
ROLLBACK_INFRASTRUCTURE_SERVICES=(db redis redis_broker egress_proxy)
ROLLBACK_APPLICATION_SERVICES=(django celery_worker celery_beat celery_worker_images frontend nginx)
DRAIN_SERVICES=(celery_beat celery_worker celery_worker_images django frontend)
HEALTH_SERVICES=(db redis redis_broker egress_proxy django celery_worker celery_beat celery_worker_images frontend nginx)
LOG_SERVICES=(db redis redis_broker egress_proxy django celery_worker celery_beat celery_worker_images frontend nginx)

PROD_HEALTH_RETRIES="${PROD_HEALTH_RETRIES:-40}"
PROD_HEALTH_INTERVAL_SECONDS="${PROD_HEALTH_INTERVAL_SECONDS:-3}"
PROD_LOG_TAIL="${PROD_LOG_TAIL:-200}"
PROD_MIN_FREE_DISK_MB="${PROD_MIN_FREE_DISK_MB:-16384}"
PROD_ROLLBACK_ENABLED="${PROD_ROLLBACK_ENABLED:-true}"
PROD_BROKER_MIGRATION_CONFIRMED="${PROD_BROKER_MIGRATION_CONFIRMED:-false}"
PROD_BACKUP_TIMEOUT_SECONDS="${PROD_BACKUP_TIMEOUT_SECONDS:-7200}"
PROD_DRAIN_TIMEOUT_SECONDS="${PROD_DRAIN_TIMEOUT_SECONDS:-3700}"
PROD_SMOKE_URL="${PROD_SMOKE_URL:-}"
DEPLOY_PHASE="initialization"
SERVICES_CHANGED=false
MIGRATIONS_APPLIED=false
MIGRATIONS_STARTED=false
declare -A ROLLBACK_IMAGE_IDS=()
declare -A ROLLBACK_IMAGE_NAMES=()

show_logs() {
  echo ""
  echo "==> Последние логи Compose (tail=${PROD_LOG_TAIL}):"
  "${COMPOSE[@]}" logs --tail="$PROD_LOG_TAIL" "${LOG_SERVICES[@]}" || true
}

wait_for_service() {
  local service="$1"
  local attempt container_id state health

  for ((attempt = 1; attempt <= PROD_HEALTH_RETRIES; attempt++)); do
    container_id="$("${COMPOSE[@]}" ps -q "$service" 2>/dev/null || true)"
    if [[ -n "$container_id" ]]; then
      state="$(docker inspect --format '{{.State.Status}}' "$container_id" 2>/dev/null || true)"
      health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || true)"

      if [[ "$state" == "running" && ("$health" == "healthy" || "$health" == "none") ]]; then
        echo "    ${service}: ${health/none/running}"
        return 0
      fi
      # A failed early health probe is recoverable: Docker can move a service
      # from unhealthy back to healthy after the process finishes starting.
      if [[ "$state" == "exited" || "$state" == "dead" ]]; then
        fail "сервис ${service} перешёл в состояние state=${state}, health=${health}."
        return 1
      fi
    fi
    sleep "$PROD_HEALTH_INTERVAL_SECONDS"
  done

  fail "сервис ${service} не стал готов за отведённое время."
}

ensure_current_infrastructure() {
  local service container_id
  local existing=0
  local infrastructure=(db redis redis_broker egress_proxy)

  for service in "${infrastructure[@]}"; do
    container_id="$("${COMPOSE[@]}" ps -q "$service" 2>/dev/null || true)"
    [[ -z "$container_id" ]] || existing=$((existing + 1))
  done
  if (( existing == 0 )); then
    echo "==> Первичный запуск инфраструктуры..."
    SERVICES_CHANGED=true
    "${COMPOSE[@]}" up -d --no-build "${infrastructure[@]}"
  elif (( existing != ${#infrastructure[@]} )); then
    fail "инфраструктура запущена частично; автоматическое recreate до drain запрещено."
  fi
  for service in "${infrastructure[@]}"; do
    wait_for_service "$service"
  done
}

capture_rollback_images() {
  local service container_id image_id image_name

  for service in "${ROLLBACK_SERVICES[@]}"; do
    container_id="$("${COMPOSE[@]}" ps -q "$service" 2>/dev/null || true)"
    [[ -n "$container_id" ]] || continue

    image_id="$(docker inspect --format '{{.Image}}' "$container_id" 2>/dev/null || true)"
    image_name="$(docker inspect --format '{{.Config.Image}}' "$container_id" 2>/dev/null || true)"
    if [[ -n "$image_id" && -n "$image_name" ]]; then
      ROLLBACK_IMAGE_IDS["$service"]="$image_id"
      ROLLBACK_IMAGE_NAMES["$service"]="$image_name"
    fi
  done
}

probe_redis_writes_after_rollback() {
  # shellcheck disable=SC2016  # Expand credentials inside each container.
  "${COMPOSE[@]}" exec -T redis sh -ec '
    probe="saas-poster:rollback-probe:$$"
    test "$(REDISCLI_AUTH="$CACHE_REDIS_PASSWORD" redis-cli SET "$probe" 1 EX 10 NX)" = OK
    test "$(REDISCLI_AUTH="$CACHE_REDIS_PASSWORD" redis-cli DEL "$probe")" = 1
  '
  # shellcheck disable=SC2016  # Expand credentials inside each container.
  "${COMPOSE[@]}" exec -T redis_broker sh -ec '
    probe="saas-poster:rollback-probe:$$"
    test "$(REDISCLI_AUTH="$CELERY_REDIS_PASSWORD" redis-cli SET "$probe" 1 EX 10 NX)" = OK
    test "$(REDISCLI_AUTH="$CELERY_REDIS_PASSWORD" redis-cli DEL "$probe")" = 1
  '
}

rollback_deployment() {
  local exit_code="$1"
  local service rollback_failed=false

  trap - ERR HUP INT TERM
  set +e
  echo ""
  echo "==> Деплой прерван на этапе '${DEPLOY_PHASE}' (exit=${exit_code})." >&2
  show_logs

  if [[ "$MIGRATIONS_STARTED" == "true" ]]; then
    # A failure may happen after the new writers were already started (for
    # example during topology or external smoke validation).  Fail closed:
    # never leave a partially verified release accepting traffic or jobs.
    "${COMPOSE[@]}" stop -t 30 nginx || true
    "${COMPOSE[@]}" stop -t "$PROD_DRAIN_TIMEOUT_SECONDS" \
      "${DRAIN_SERVICES[@]}" || true
    echo "КРИТИЧЕСКАЯ ОШИБКА: миграция БД уже началась; старый application release автоматически не запускается." >&2
    echo "Оставьте writers остановленными, проверьте django_migrations/backup и выполните forward recovery по runbook." >&2
  elif [[ "$SERVICES_CHANGED" == "true" && "$PROD_ROLLBACK_ENABLED" == "true" && "$PREVIOUS_SHA" != "$TARGET_SHA" && "$PREVIOUS_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "==> Возврат application-сервисов на ${PREVIOUS_SHA}..."
    for service in "${ROLLBACK_SERVICES[@]}"; do
      if [[ -n "${ROLLBACK_IMAGE_IDS[$service]:-}" && -n "${ROLLBACK_IMAGE_NAMES[$service]:-}" ]]; then
        docker image tag "${ROLLBACK_IMAGE_IDS[$service]}" "${ROLLBACK_IMAGE_NAMES[$service]}" || rollback_failed=true
      fi
    done

    git checkout --detach "$PREVIOUS_SHA" || rollback_failed=true
    "${COMPOSE[@]}" config --quiet || rollback_failed=true
    # Target resource-policy changes may already have recreated persistent
    # infrastructure. Restore the old Compose contract and prove Redis writes
    # before any old application writer is allowed to start.
    "${COMPOSE[@]}" up -d --no-build --no-deps --force-recreate \
      "${ROLLBACK_INFRASTRUCTURE_SERVICES[@]}" || rollback_failed=true
    for service in "${ROLLBACK_INFRASTRUCTURE_SERVICES[@]}"; do
      wait_for_service "$service" || rollback_failed=true
    done
    probe_redis_writes_after_rollback || rollback_failed=true
    if [[ "$rollback_failed" == "false" ]]; then
      "${COMPOSE[@]}" up -d --no-build --no-deps --force-recreate \
        "${ROLLBACK_APPLICATION_SERVICES[@]}" || rollback_failed=true
      if [[ "$rollback_failed" == "false" ]]; then
        for service in "${ROLLBACK_APPLICATION_SERVICES[@]}"; do
          wait_for_service "$service" || rollback_failed=true
        done
      fi
    fi
    if [[ "$rollback_failed" == "false" ]]; then
      smoke_check || rollback_failed=true
    fi

    if [[ "$MIGRATIONS_APPLIED" == "true" ]]; then
      echo "ВНИМАНИЕ: миграции БД не откатывались; deploy-миграции обязаны быть backward-compatible." >&2
    fi
    if [[ "$rollback_failed" == "false" ]]; then
      echo "==> Application rollback завершён."
    else
      echo "КРИТИЧЕСКАЯ ОШИБКА: автоматический rollback завершился не полностью; требуется оператор." >&2
    fi
  elif [[ "$SERVICES_CHANGED" == "false" && "$PREVIOUS_SHA" != "$TARGET_SHA" && "$PREVIOUS_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    git checkout --detach "$PREVIOUS_SHA" || true
    echo "==> Runtime не изменялся; рабочая копия возвращена на ${PREVIOUS_SHA}."
  else
    echo "==> Автоматический rollback недоступен или отключён; runtime оставлен для диагностики." >&2
  fi

  exit "$exit_code"
}

trap 'rollback_deployment $?' ERR
trap 'rollback_deployment 129' HUP
trap 'rollback_deployment 130' INT
trap 'rollback_deployment 143' TERM

validate_integer_setting() {
  local name="$1"
  local value="$2"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || fail "${name} должен быть положительным целым числом."
}

preflight() {
  local command_name available_kb required_kb

  DEPLOY_PHASE="preflight"
  for command_name in git docker curl flock df awk stat timeout cmp; do
    command -v "$command_name" >/dev/null 2>&1 || fail "команда ${command_name} не установлена."
  done

  validate_integer_setting PROD_HEALTH_RETRIES "$PROD_HEALTH_RETRIES"
  validate_integer_setting PROD_HEALTH_INTERVAL_SECONDS "$PROD_HEALTH_INTERVAL_SECONDS"
  validate_integer_setting PROD_LOG_TAIL "$PROD_LOG_TAIL"
  validate_integer_setting PROD_MIN_FREE_DISK_MB "$PROD_MIN_FREE_DISK_MB"
  (( PROD_MIN_FREE_DISK_MB >= 8192 )) \
    || fail "PROD_MIN_FREE_DISK_MB нельзя задавать ниже 8192 MiB."
  validate_integer_setting PROD_BACKUP_TIMEOUT_SECONDS "$PROD_BACKUP_TIMEOUT_SECONDS"
  validate_integer_setting PROD_DRAIN_TIMEOUT_SECONDS "$PROD_DRAIN_TIMEOUT_SECONDS"
  [[ "$PROD_ROLLBACK_ENABLED" == "true" || "$PROD_ROLLBACK_ENABLED" == "false" ]] \
    || fail "PROD_ROLLBACK_ENABLED должен быть true или false."
  [[ "$PROD_BROKER_MIGRATION_CONFIRMED" == "true" ]] \
    || fail "сначала выполните drain legacy Celery queue и установите PROD_BROKER_MIGRATION_CONFIRMED=true."

  [[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] \
    || fail "deploy.sh ожидает полный 40-символьный commit SHA."
  [[ "$PREVIOUS_SHA" =~ ^[0-9a-f]{40}$ ]] \
    || fail "PREVIOUS_SHA обязателен и должен быть сохранён до checkout TARGET_SHA."
  [[ "$(git rev-parse HEAD)" == "$TARGET_SHA" ]] \
    || fail "рабочая копия должна быть переключена на TARGET_SHA до запуска deploy.sh."
  if [[ -n "$(git status --porcelain=v1 --untracked-files=normal --ignore-submodules=none)" ]]; then
    fail "production checkout содержит tracked или untracked изменения; image build не будет соответствовать TARGET_SHA."
  fi

  git fetch --no-tags origin main
  git cat-file -e "${TARGET_SHA}^{commit}"
  git cat-file -e "${PREVIOUS_SHA}^{commit}"
  git merge-base --is-ancestor "$PREVIOUS_SHA" "$TARGET_SHA" \
    || fail "PREVIOUS_SHA должен быть предком TARGET_SHA."
  git merge-base --is-ancestor "$TARGET_SHA" origin/main \
    || fail "commit ${TARGET_SHA} не принадлежит актуальной ветке origin/main."

  if [[ "$ROOT_DIR" == "/opt/saas_poster" ]]; then
    verify_installed_host_contract
    validate_pending_host_contract
  fi

  validate_private_regular_file "$ROOT_DIR/.env"
  validate_private_regular_file "$ROOT_DIR/.backup.env"
  if [[ -e "$DEPLOY_ENV_FILE" || -L "$DEPLOY_ENV_FILE" ]]; then
    validate_private_regular_file "$DEPLOY_ENV_FILE"
  fi
  [[ "$PROD_SMOKE_URL" =~ ^https://[^[:space:]]+$ ]] \
    || fail "PROD_SMOKE_URL обязателен и должен быть публичным HTTPS URL."

  docker info >/dev/null
  "${COMPOSE[@]}" version >/dev/null
  "${COMPOSE[@]}" config --quiet

  available_kb="$(df -Pk "$ROOT_DIR" | awk 'NR == 2 {print $4}')"
  required_kb="$((PROD_MIN_FREE_DISK_MB * 1024))"
  [[ "$available_kb" =~ ^[0-9]+$ && "$available_kb" -ge "$required_kb" ]] \
    || fail "недостаточно свободного места: требуется минимум ${PROD_MIN_FREE_DISK_MB} MiB."
}

drain_application_writers() {
  DEPLOY_PHASE="billing maintenance drain"
  echo "==> Остановка входящего трафика и drain старых application writers..."
  # Сначала закрываем ingress и scheduler: после этого новые checkout/webhook и
  # периодические billing-задачи не появятся. Workers/Gunicorn получают TERM и
  # должны завершить текущую работу до schema migration.
  "${COMPOSE[@]}" stop -t 30 nginx
  "${COMPOSE[@]}" stop -t "$PROD_DRAIN_TIMEOUT_SECONDS" "${DRAIN_SERVICES[@]}"
}

smoke_check() {
  local attempt

  DEPLOY_PHASE="external smoke check"
  for ((attempt = 1; attempt <= PROD_HEALTH_RETRIES; attempt++)); do
    if curl --fail --silent --show-error --max-time 10 --output /dev/null "$PROD_SMOKE_URL"; then
      echo "    external: ${PROD_SMOKE_URL}"
      return 0
    fi
    sleep "$PROD_HEALTH_INTERVAL_SECONDS"
  done
  fail "внешний smoke-check не прошёл: ${PROD_SMOKE_URL}."
}

echo "==> Preflight для commit ${TARGET_SHA}..."
preflight

if [[ "$ROOT_DIR" == "/opt/saas_poster" ]]; then
  DEPLOY_PHASE="host capacity"
  "$ROOT_DIR/scripts/check_production_capacity.sh"
fi

echo "==> Сохранение образов текущего release для rollback..."
capture_rollback_images

DEPLOY_PHASE="infrastructure readiness"
echo "==> Сборка patched egress proxy до изменения работающих сервисов..."
"${COMPOSE[@]}" build --pull egress_proxy
echo "==> Проверка работающих инфраструктурных зависимостей..."
ensure_current_infrastructure

DEPLOY_PHASE="application build"
echo "==> Сборка application-образов до изменения работающих сервисов..."
"${COMPOSE[@]}" --profile ops build --pull "${BUILD_SERVICES[@]}"

DEPLOY_PHASE="Django pre-deploy checks"
echo "==> Проверка новой Django-сборки..."
"${COMPOSE[@]}" run --rm --no-deps django python manage.py check --deploy
"${COMPOSE[@]}" run --rm --no-deps django python manage.py makemigrations --check --dry-run
"${COMPOSE[@]}" run --rm --no-deps django python manage.py migrate --plan

DEPLOY_PHASE="runtime dependency connectivity"
echo "==> Проверка Redis connectivity из нового Django-образа..."
timeout --foreground --signal=TERM --kill-after=5s 60s \
  "${COMPOSE[@]}" run --rm --no-deps django \
    python manage.py check_redis_connectivity

echo "==> Проверка SMTP connectivity и credentials из нового Django-образа..."
timeout --foreground --signal=TERM --kill-after=5s 60s \
  "${COMPOSE[@]}" run --rm --no-deps django \
    python manage.py check_email_connectivity

echo "==> Проверка public HTTPS transport и, если billing включён, YooKassa credentials..."
timeout --foreground --signal=TERM --kill-after=5s 60s \
  "${COMPOSE[@]}" run --rm --no-deps django \
    python manage.py check_public_http_connectivity

# The first stop is already a runtime mutation.  Set the rollback guard before
# entering the drain function so ERR/HUP/INT/TERM at either stop cannot leave a
# partially drained previous release down while claiming that runtime was
# untouched.
SERVICES_CHANGED=true
drain_application_writers

DEPLOY_PHASE="egress proxy rollout"
echo "==> Переключение egress proxy после graceful drain writers..."
"${COMPOSE[@]}" up -d --no-build db redis redis_broker egress_proxy
wait_for_service db
wait_for_service redis
wait_for_service redis_broker
wait_for_service egress_proxy

DEPLOY_PHASE="target egress connectivity"
echo "==> Проверка writable Redis и target memory limits после recreate..."
timeout --foreground --signal=TERM --kill-after=5s 60s \
  "${COMPOSE[@]}" run --rm --no-deps django \
    python manage.py check_redis_connectivity --require-target-limits
echo "==> Проверка SMTP через новый egress proxy до миграций..."
timeout --foreground --signal=TERM --kill-after=5s 60s \
  "${COMPOSE[@]}" run --rm --no-deps django \
    python manage.py check_email_connectivity
echo "==> Проверка public HTTPS через новый egress proxy до миграций..."
timeout --foreground --signal=TERM --kill-after=5s 60s \
  "${COMPOSE[@]}" run --rm --no-deps django \
    python manage.py check_public_http_connectivity

DEPLOY_PHASE="pre-migration database backup"
echo "==> Создание зашифрованного backup перед миграциями..."
timeout --foreground --signal=TERM --kill-after=60s \
  "${PROD_BACKUP_TIMEOUT_SECONDS}s" \
  "${COMPOSE[@]}" --profile ops run --rm --no-deps \
    -e DEPLOY_GIT_SHA="$TARGET_SHA" backup

DEPLOY_PHASE="database migration"
echo "==> Применение миграций при остановленных старых writers..."
MIGRATIONS_STARTED=true
"${COMPOSE[@]}" run --rm --no-deps django python manage.py migrate --noinput
MIGRATIONS_APPLIED=true

DEPLOY_PHASE="release data preparation"
echo "==> Подготовка статики и служебных данных..."
"${COMPOSE[@]}" run --rm --no-deps django python manage.py seed_plans
"${COMPOSE[@]}" run --rm --no-deps django python manage.py collectstatic --noinput
"${COMPOSE[@]}" run --rm --no-deps django python manage.py setup_periodic_tasks
"${COMPOSE[@]}" run --rm --no-deps django python manage.py seed_tenant_categories

DEPLOY_PHASE="application rollout"
echo "==> Переключение application-сервисов на новый release..."
"${COMPOSE[@]}" up -d --no-build --remove-orphans

DEPLOY_PHASE="service readiness"
echo "==> Проверка готовности сервисов..."
for service in "${HEALTH_SERVICES[@]}"; do
  wait_for_service "$service"
done

DEPLOY_PHASE="runtime topology verification"
"$ROOT_DIR/scripts/verify_production_topology.sh"

smoke_check

if [[ "$ROOT_DIR" == "/opt/saas_poster" ]]; then
  DEPLOY_PHASE="backup timer activation"
  systemctl enable --now saas-poster-backup.timer \
    saas-poster-backup-check.timer
  systemctl is-active --quiet saas-poster-backup.timer \
    saas-poster-backup-check.timer \
    || fail "backup timers не перешли в active после rollout."
  rm -f -- "$HOST_CONTRACT_PENDING_FILE"
fi

trap - ERR HUP INT TERM
echo ""
echo "==> Деплой ${TARGET_SHA} успешно завершён."
"${COMPOSE[@]}" ps
