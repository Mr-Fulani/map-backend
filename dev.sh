#!/usr/bin/env bash
# Запуск всего проекта одной командой: Docker-сервисы + Next.js dev server
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Docker-сервисы (бэкенд, PostgreSQL, Redis, Celery) ---
echo "==> Запуск Docker-сервисов..."
docker compose -f "$ROOT_DIR/docker-compose.yml" up -d

# Ждём готовности Django
echo "==> Ожидание Django (http://localhost:8000)..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:8000/api/health/ > /dev/null 2>&1; then
    echo "    Django готов."
    break
  fi
  sleep 2
done

# --- Next.js dev server ---
echo "==> Запуск фронтенда (Next.js)..."
cd "$ROOT_DIR/frontend"
npm run dev &
FRONTEND_PID=$!

echo ""
echo "  Проект запущен:"
echo "    Backend:  http://localhost:8000"
echo "    Frontend: http://localhost:3000"
echo "    Swagger:  http://localhost:8000/api/docs/"
echo ""
echo "  Для остановки нажмите Ctrl+C"

# При выходе останавливаем фронтенд
trap "echo '==> Остановка фронтенда...'; kill $FRONTEND_PID 2>/dev/null; exit 0" INT TERM
wait $FRONTEND_PID
