#!/usr/bin/env bash
# Перезапуск всего проекта: stop → cleanup → Docker → миграции → Next.js → live logs
set -e

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
    echo "  --fast   быстрый перезапуск, лёгкая очистка мусора (по умолчанию)"
    echo "  --clean  глубокий перезапуск с очисткой build/cache мусора"
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

stop_port_process() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    local pids
    pids="$(lsof -ti "tcp:${port}" 2>/dev/null || true)"
    if [ -n "$pids" ]; then
      echo "==> Остановка процессов на порту ${port}..."
      kill $pids 2>/dev/null || true
      for _ in $(seq 1 20); do
        sleep 0.2
        pids="$(lsof -ti "tcp:${port}" 2>/dev/null || true)"
        if [ -z "$pids" ]; then
          return
        fi
      done
      echo "==> Принудительная остановка процессов на порту ${port}..."
      kill -9 $pids 2>/dev/null || true
    fi
  fi
}

# ── 1. Полная остановка текущего окружения ────────────────────────────────────
echo "==> Остановка текущих сервисов..."
docker compose down --remove-orphans
stop_port_process 3000

# ── 2. Очистка мусора (volumes не трогаем — там БД) ──────────────────────────
if [ "$MODE" = "clean" ]; then
  echo "==> Глубокая очистка контейнерного и frontend-кеша..."
  docker container prune -f > /dev/null
  docker image prune -af > /dev/null
  docker builder prune -af > /dev/null
  rm -rf "$ROOT_DIR/frontend/.next"
else
  echo "==> Быстрая очистка остановленных контейнеров и висячих образов..."
  docker container prune -f > /dev/null
  docker image prune -f > /dev/null
  docker builder prune -f --filter "until=24h" > /dev/null
  rm -rf "$ROOT_DIR/frontend/.next/static"
fi

# BuildKit переиспользует кэш, но пересобирает образы при изменении Dockerfile или requirements.
echo "==> Проверка backend-образов..."
docker compose build "${BACKEND_BUILD_SERVICES[@]}"

# ── 3. БД и Redis — сначала ───────────────────────────────────────────────────
echo "==> Запуск db, redis..."
docker compose up -d db redis

echo "==> Ожидание готовности PostgreSQL..."
for i in $(seq 1 30); do
  if docker compose exec -T db pg_isready -q 2>/dev/null; then
    echo "    PostgreSQL готов."
    break
  fi
  if [ "$i" -eq 30 ]; then echo "    ОШИБКА: PostgreSQL не запустился."; exit 1; fi
  sleep 2
done

# ── 4. Django ─────────────────────────────────────────────────────────────────
echo "==> Запуск django..."
docker compose up -d django

echo "==> Ожидание готовности Django..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:8000/api/v1/health/ > /dev/null 2>&1; then
    echo "    Django готов."
    break
  fi
  if [ "$i" -eq 30 ]; then echo "    ОШИБКА: Django не запустился."; exit 1; fi
  sleep 2
done

# ── 5. Миграции ───────────────────────────────────────────────────────────────
echo "==> Применение миграций..."
docker compose exec -T django python manage.py migrate

# ── 6. Остальные backend-сервисы ──────────────────────────────────────────────
echo "==> Запуск celery..."
docker compose up -d celery_worker celery_beat celery_worker_images

# ── 7. Next.js dev server ─────────────────────────────────────────────────────
echo "==> Запуск фронтенда (Next.js)..."
cd "$ROOT_DIR/frontend"
npm run dev &
FRONTEND_PID=$!

echo ""
echo "  Проект запущен:"
echo "    Frontend: http://localhost:3000"
echo "    Backend:  http://localhost:8000"
echo "    Swagger:  http://localhost:8000/api/docs/"
echo ""
echo "  Для остановки нажмите Ctrl+C"

cleanup() {
  echo "==> Остановка фоновых процессов..."
  kill "$FRONTEND_PID" 2>/dev/null || true
  exit 0
}

trap cleanup INT TERM

echo ""
echo "==> Live logs всех сервисов Docker Compose (tail=${LOG_TAIL}) + frontend:"
docker compose logs --tail="$LOG_TAIL" -f "${COMPOSE_LOG_SERVICES[@]}" || true
cleanup
