#!/usr/bin/env bash
# Production deploy for one immutable commit that has already passed CI.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

fail() {
  echo "ОШИБКА: $*" >&2
  return 1
}

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

TARGET_SHA="${1:-}"
DEPLOY_ENV_FILE="${DEPLOY_ENV_FILE:-$ROOT_DIR/.deploy.env}"
if [[ -e "$DEPLOY_ENV_FILE" || -L "$DEPLOY_ENV_FILE" ]]; then
  command -v stat >/dev/null 2>&1 || fail "команда stat не установлена."
  validate_private_regular_file "$DEPLOY_ENV_FILE"
  load_deploy_env "$DEPLOY_ENV_FILE"
fi

COMPOSE_FILE="$ROOT_DIR/docker-compose.prod.yml"
PROD_LOCK_DIR="/run/lock/saas-poster"
DEPLOY_LOCK_FILE="$PROD_LOCK_DIR/deploy.lock"
COMPOSE=(
  docker compose
  --project-name saas_poster
  --project-directory "$ROOT_DIR"
  -f "$COMPOSE_FILE"
)
BUILD_SERVICES=(django celery_worker celery_beat celery_worker_images frontend backup)
APPLICATION_SERVICES=(django celery_worker celery_beat celery_worker_images frontend nginx)
DRAIN_SERVICES=(celery_beat celery_worker celery_worker_images django frontend)
HEALTH_SERVICES=(db redis redis_broker egress_proxy django celery_worker celery_beat celery_worker_images frontend nginx)
LOG_SERVICES=(db redis redis_broker egress_proxy django celery_worker celery_beat celery_worker_images frontend nginx)

PROD_HEALTH_RETRIES="${PROD_HEALTH_RETRIES:-40}"
PROD_HEALTH_INTERVAL_SECONDS="${PROD_HEALTH_INTERVAL_SECONDS:-3}"
PROD_LOG_TAIL="${PROD_LOG_TAIL:-200}"
PROD_MIN_FREE_DISK_MB="${PROD_MIN_FREE_DISK_MB:-2048}"
PROD_ROLLBACK_ENABLED="${PROD_ROLLBACK_ENABLED:-true}"
PROD_BROKER_MIGRATION_CONFIRMED="${PROD_BROKER_MIGRATION_CONFIRMED:-false}"
PROD_BACKUP_TIMEOUT_SECONDS="${PROD_BACKUP_TIMEOUT_SECONDS:-7200}"
PROD_DRAIN_TIMEOUT_SECONDS="${PROD_DRAIN_TIMEOUT_SECONDS:-3700}"
PROD_SMOKE_URL="${PROD_SMOKE_URL:-}"
PREVIOUS_SHA="${PREVIOUS_SHA:-$(git rev-parse HEAD 2>/dev/null || true)}"

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
      if [[ "$state" == "exited" || "$state" == "dead" || "$health" == "unhealthy" ]]; then
        fail "сервис ${service} перешёл в состояние state=${state}, health=${health}."
        return 1
      fi
    fi
    sleep "$PROD_HEALTH_INTERVAL_SECONDS"
  done

  fail "сервис ${service} не стал готов за отведённое время."
}

capture_rollback_images() {
  local service container_id image_id image_name

  for service in "${BUILD_SERVICES[@]}"; do
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

rollback_deployment() {
  local exit_code="$1"
  local service rollback_failed=false

  trap - ERR HUP INT TERM
  set +e
  echo ""
  echo "==> Деплой прерван на этапе '${DEPLOY_PHASE}' (exit=${exit_code})." >&2
  show_logs

  if [[ "$MIGRATIONS_STARTED" == "true" ]]; then
    echo "КРИТИЧЕСКАЯ ОШИБКА: миграция БД уже началась; старый application release автоматически не запускается." >&2
    echo "Оставьте writers остановленными, проверьте django_migrations/backup и выполните forward recovery по runbook." >&2
  elif [[ "$SERVICES_CHANGED" == "true" && "$PROD_ROLLBACK_ENABLED" == "true" && "$PREVIOUS_SHA" != "$TARGET_SHA" && "$PREVIOUS_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "==> Возврат application-сервисов на ${PREVIOUS_SHA}..."
    for service in "${BUILD_SERVICES[@]}"; do
      if [[ -n "${ROLLBACK_IMAGE_IDS[$service]:-}" && -n "${ROLLBACK_IMAGE_NAMES[$service]:-}" ]]; then
        docker image tag "${ROLLBACK_IMAGE_IDS[$service]}" "${ROLLBACK_IMAGE_NAMES[$service]}" || rollback_failed=true
      fi
    done

    git checkout --detach "$PREVIOUS_SHA" || rollback_failed=true
    "${COMPOSE[@]}" config --quiet || rollback_failed=true
    "${COMPOSE[@]}" up -d --no-build --force-recreate "${APPLICATION_SERVICES[@]}" || rollback_failed=true
    for service in "${APPLICATION_SERVICES[@]}"; do
      wait_for_service "$service" || rollback_failed=true
    done
    smoke_check || rollback_failed=true

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
  for command_name in git docker curl flock df awk stat timeout; do
    command -v "$command_name" >/dev/null 2>&1 || fail "команда ${command_name} не установлена."
  done

  validate_integer_setting PROD_HEALTH_RETRIES "$PROD_HEALTH_RETRIES"
  validate_integer_setting PROD_HEALTH_INTERVAL_SECONDS "$PROD_HEALTH_INTERVAL_SECONDS"
  validate_integer_setting PROD_LOG_TAIL "$PROD_LOG_TAIL"
  validate_integer_setting PROD_MIN_FREE_DISK_MB "$PROD_MIN_FREE_DISK_MB"
  validate_integer_setting PROD_BACKUP_TIMEOUT_SECONDS "$PROD_BACKUP_TIMEOUT_SECONDS"
  validate_integer_setting PROD_DRAIN_TIMEOUT_SECONDS "$PROD_DRAIN_TIMEOUT_SECONDS"
  [[ "$PROD_ROLLBACK_ENABLED" == "true" || "$PROD_ROLLBACK_ENABLED" == "false" ]] \
    || fail "PROD_ROLLBACK_ENABLED должен быть true или false."
  [[ "$PROD_BROKER_MIGRATION_CONFIRMED" == "true" ]] \
    || fail "сначала выполните drain legacy Celery queue и установите PROD_BROKER_MIGRATION_CONFIRMED=true."

  [[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] \
    || fail "deploy.sh ожидает полный 40-символьный commit SHA."
  [[ "$(git rev-parse HEAD)" == "$TARGET_SHA" ]] \
    || fail "рабочая копия должна быть переключена на TARGET_SHA до запуска deploy.sh."
  if [[ -n "$(git status --porcelain=v1 --untracked-files=normal --ignore-submodules=none)" ]]; then
    fail "production checkout содержит tracked или untracked изменения; image build не будет соответствовать TARGET_SHA."
  fi

  git fetch --no-tags origin main
  git cat-file -e "${TARGET_SHA}^{commit}"
  git merge-base --is-ancestor "$TARGET_SHA" origin/main \
    || fail "commit ${TARGET_SHA} не принадлежит актуальной ветке origin/main."

  validate_private_regular_file "$ROOT_DIR/.env"
  validate_private_regular_file "$ROOT_DIR/.backup.env"
  if [[ -e "$DEPLOY_ENV_FILE" || -L "$DEPLOY_ENV_FILE" ]]; then
    validate_private_regular_file "$DEPLOY_ENV_FILE"
  fi
  [[ "$PROD_SMOKE_URL" =~ ^https://[^[:space:]]+$ ]] \
    || fail "PROD_SMOKE_URL обязателен и должен быть публичным HTTPS URL."

  [[ -d "$PROD_LOCK_DIR" && ! -L "$PROD_LOCK_DIR" && -O "$PROD_LOCK_DIR" ]] \
    || fail "$PROD_LOCK_DIR должен быть обычным каталогом владельца deploy user."
  [[ "$(stat -c '%a' "$PROD_LOCK_DIR")" == "700" ]] \
    || fail "$PROD_LOCK_DIR должен иметь права 700."
  exec 9>"$DEPLOY_LOCK_FILE"
  flock -n 9 || fail "другой deploy уже выполняется."

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

echo "==> Сохранение образов текущего release для rollback..."
capture_rollback_images

DEPLOY_PHASE="infrastructure readiness"
SERVICES_CHANGED=true
echo "==> Запуск инфраструктурных зависимостей..."
"${COMPOSE[@]}" up -d --no-build db redis redis_broker egress_proxy
wait_for_service db
wait_for_service redis
wait_for_service redis_broker
wait_for_service egress_proxy

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

echo "==> Проверка public HTTPS transport и YooKassa credentials..."
timeout --foreground --signal=TERM --kill-after=5s 60s \
  "${COMPOSE[@]}" run --rm --no-deps django \
    python manage.py check_public_http_connectivity

drain_application_writers

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

smoke_check

trap - ERR HUP INT TERM
echo ""
echo "==> Деплой ${TARGET_SHA} успешно завершён."
"${COMPOSE[@]}" ps
