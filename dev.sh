#!/usr/bin/env bash
# Перезапуск только сервисов этого Compose-проекта и локального Next.js.
# Скрипт намеренно не завершает чужие процессы и не выполняет Docker prune.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

MODE="fast"
case "${1:-}" in
  ""|"--fast")
    MODE="fast"
    ;;
  "--clean"|"--deep")
    MODE="clean"
    ;;
  "-h"|"--help")
    echo "Использование: ./dev.sh [--fast|--clean]"
    echo "  --fast   перезапуск без удаления локального frontend-кеша (по умолчанию)"
    echo "  --clean  дополнительно удалить только frontend/.next этого проекта"
    exit 0
    ;;
  *)
    echo "Использование: ./dev.sh [--fast|--clean]"
    exit 1
    ;;
esac

LOG_TAIL="${DEV_LOG_TAIL:-200}"
COMPOSE_LOG_SERVICES=(db redis django celery_worker celery_beat celery_worker_images)
BACKEND_BUILD_SERVICES=(django celery_worker celery_beat celery_worker_images)
COMPOSE=(
  docker compose
  --project-name saas_poster
  --project-directory "$ROOT_DIR"
  -f "$ROOT_DIR/docker-compose.yml"
)
FRONTEND_PID=""

cleanup() {
  local exit_code=$?
  trap - EXIT HUP INT TERM
  set +e

  if [[ -n "$FRONTEND_PID" ]] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
    echo "==> Остановка frontend-процессов этого запуска..."
    # Job control below gives npm/Next an isolated process group. Signalling the
    # owned group prevents orphaned Next children without touching foreign PIDs.
    kill -TERM -- "-$FRONTEND_PID" 2>/dev/null \
      || kill -TERM "$FRONTEND_PID" 2>/dev/null \
      || true
    for _ in $(seq 1 20); do
      kill -0 "$FRONTEND_PID" 2>/dev/null || break
      sleep 0.25
    done
    wait "$FRONTEND_PID" 2>/dev/null || true
  fi

  echo "==> Остановка сервисов текущего Compose-проекта..."
  "${COMPOSE[@]}" down --remove-orphans || true
  exit "$exit_code"
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

require_free_port() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "ОШИБКА: TCP-порт ${port} уже занят." >&2
      echo "dev.sh не завершает чужие процессы. Освободите порт вручную и повторите запуск:" >&2
      lsof -nP -iTCP:"$port" -sTCP:LISTEN >&2 || true
      exit 1
    fi
    return
  fi

  if command -v ss >/dev/null 2>&1; then
    if ss -H -ltn "sport = :${port}" 2>/dev/null | grep -q .; then
      echo "ОШИБКА: TCP-порт ${port} уже занят." >&2
      echo "dev.sh не завершает чужие процессы. Освободите порт вручную и повторите запуск." >&2
      exit 1
    fi
    return
  fi

  echo "ОШИБКА: не найден lsof или ss; безопасно проверить TCP-порт ${port} невозможно." >&2
  exit 1
}

# ── 1. Остановка только текущего Compose-проекта и безопасная проверка ────────
echo "==> Остановка сервисов текущего Compose-проекта..."
"${COMPOSE[@]}" down --remove-orphans
for port in 3000 8000 5432 6379; do
  require_free_port "$port"
done

# ── 2. Опциональная очистка только локального frontend-кеша ──────────────────
if [ "$MODE" = "clean" ]; then
  echo "==> Удаление frontend/.next этого проекта..."
  rm -rf "$ROOT_DIR/frontend/.next"
else
  echo "==> Локальный frontend-кеш сохранён."
fi

# BuildKit переиспользует кэш, но пересобирает образы при изменении Dockerfile или requirements.
echo "==> Проверка backend-образов..."
"${COMPOSE[@]}" build "${BACKEND_BUILD_SERVICES[@]}"

# ── 3. БД и Redis — сначала ───────────────────────────────────────────────────
echo "==> Запуск db, redis..."
"${COMPOSE[@]}" up -d db redis

echo "==> Ожидание готовности PostgreSQL..."
for i in $(seq 1 30); do
  if "${COMPOSE[@]}" exec -T db pg_isready -q 2>/dev/null; then
    echo "    PostgreSQL готов."
    break
  fi
  if [ "$i" -eq 30 ]; then echo "    ОШИБКА: PostgreSQL не запустился."; exit 1; fi
  sleep 2
done

# ── 4. Миграции до публикации Django ─────────────────────────────────────────
echo "==> Применение миграций..."
"${COMPOSE[@]}" run --rm django python manage.py migrate

echo "==> Загрузка тарифных планов..."
"${COMPOSE[@]}" run --rm django python manage.py seed_plans

echo "==> Настройка периодических задач..."
"${COMPOSE[@]}" run --rm django python manage.py setup_periodic_tasks

# ── 5. Django ─────────────────────────────────────────────────────────────────
echo "==> Запуск django..."
"${COMPOSE[@]}" up -d django

echo "==> Ожидание готовности Django..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:8000/api/v1/ready/ > /dev/null 2>&1; then
    echo "    Django готов."
    break
  fi
  if [ "$i" -eq 30 ]; then echo "    ОШИБКА: Django не готов к работе."; exit 1; fi
  sleep 2
done

# ── 6. Остальные backend-сервисы ──────────────────────────────────────────────
echo "==> Запуск celery..."
"${COMPOSE[@]}" up -d celery_worker celery_beat celery_worker_images

# ── 7. Next.js dev server ─────────────────────────────────────────────────────
echo "==> Запуск фронтенда (Next.js)..."
cd "$ROOT_DIR/frontend"
set -m
NEXT_PUBLIC_API_URL="${NEXT_PUBLIC_API_URL:-http://localhost:8000}" npm run dev &
FRONTEND_PID=$!
set +m

echo "==> Ожидание готовности frontend..."
for i in $(seq 1 60); do
  if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    wait "$FRONTEND_PID" || true
    echo "    ОШИБКА: frontend завершился до readiness." >&2
    exit 1
  fi
  if curl -sf --max-time 2 http://localhost:3000/ > /dev/null 2>&1; then
    echo "    Frontend готов."
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "    ОШИБКА: frontend не стал готов за 60 секунд." >&2
    exit 1
  fi
  sleep 1
done

echo ""
echo "  Проект запущен:"
echo "    Frontend: http://localhost:3000"
echo "    Backend:  http://localhost:8000"
echo "    Swagger:  http://localhost:8000/api/docs/"
echo ""
echo "  Ctrl+C остановит frontend и сервисы только этого Compose-проекта."

echo ""
echo "==> Live logs всех сервисов Docker Compose (tail=${LOG_TAIL}) + frontend:"
"${COMPOSE[@]}" logs --tail="$LOG_TAIL" -f "${COMPOSE_LOG_SERVICES[@]}"
