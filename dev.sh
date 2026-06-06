#!/usr/bin/env bash
# Запуск всего проекта: очистка мусора → Docker → миграции → Next.js
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

# ── 1. Очистка мусора (volumes не трогаем — там БД) ──────────────────────────
echo "==> Очистка остановленных контейнеров и висячих образов..."
docker container prune -f > /dev/null
docker image prune -f > /dev/null
docker builder prune -f --filter "until=24h" > /dev/null

# ── 2. БД и Redis — сначала ───────────────────────────────────────────────────
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

# ── 3. Django ─────────────────────────────────────────────────────────────────
echo "==> Запуск django..."
docker compose up -d django

echo "==> Ожидание готовности Django..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:8000/api/health/ > /dev/null 2>&1; then
    echo "    Django готов."
    break
  fi
  if [ "$i" -eq 30 ]; then echo "    ОШИБКА: Django не запустился."; exit 1; fi
  sleep 2
done

# ── 4. Миграции ───────────────────────────────────────────────────────────────
echo "==> Применение миграций..."
docker compose exec -T django python manage.py migrate

# ── 5. Остальные сервисы ──────────────────────────────────────────────────────
echo "==> Запуск celery, nginx, frontend..."
docker compose up -d

# ── 6. Next.js dev server ─────────────────────────────────────────────────────
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

trap "echo '==> Остановка фронтенда...'; kill $FRONTEND_PID 2>/dev/null; exit 0" INT TERM
wait $FRONTEND_PID
