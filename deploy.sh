#!/usr/bin/env bash
# Деплой на продакшн: git pull → очистка → Docker (prod) → миграции → collectstatic
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"
COMPOSE="docker compose -f docker-compose.prod.yml"
LOG_TAIL="${PROD_LOG_TAIL:-200}"
COMPOSE_LOG_SERVICES=(db redis egress_proxy django celery_worker celery_beat celery_worker_images frontend nginx)

show_logs() {
  echo ""
  echo "==> Последние логи Docker Compose (tail=${LOG_TAIL}):"
  $COMPOSE logs --tail="$LOG_TAIL" "${COMPOSE_LOG_SERVICES[@]}" || true
}

# ── 1. Подтянуть код ──────────────────────────────────────────────────────────
echo "==> git pull..."
git pull origin main

echo "==> Проверка production-конфигурации и обязательных секретов..."
$COMPOSE config --quiet

# ── 2. Очистка мусора (volumes не трогаем — там БД и статика) ─────────────────
echo "==> Очистка остановленных контейнеров и висячих образов..."
docker container prune -f > /dev/null
docker image prune -f > /dev/null
docker builder prune -f --filter "until=24h" > /dev/null

# ── 3. БД и Redis ─────────────────────────────────────────────────────────────
echo "==> Запуск db, redis..."
$COMPOSE up -d db redis

echo "==> Ожидание готовности PostgreSQL..."
for i in $(seq 1 30); do
  if $COMPOSE exec -T db pg_isready -q 2>/dev/null; then
    echo "    PostgreSQL готов."
    break
  fi
  if [ "$i" -eq 30 ]; then echo "    ОШИБКА: PostgreSQL не запустился."; exit 1; fi
  sleep 2
done

# ── 4. Django ─────────────────────────────────────────────────────────────────
echo "==> Сборка и запуск django..."
$COMPOSE up -d --build django

echo "==> Ожидание готовности Django..."
for i in $(seq 1 40); do
  if $COMPOSE exec -T django python manage.py check --deploy > /dev/null 2>&1; then
    echo "    Django готов."
    break
  fi
  if [ "$i" -eq 40 ]; then echo "    ОШИБКА: Django не запустился."; show_logs; exit 1; fi
  sleep 3
done

# ── 5. Миграции и статика ─────────────────────────────────────────────────────
echo "==> Применение миграций..."
$COMPOSE exec -T django python manage.py migrate

echo "==> Настройка периодических задач..."
$COMPOSE exec -T django python manage.py setup_periodic_tasks

echo "==> Сбор статики..."
$COMPOSE exec -T django python manage.py collectstatic --noinput > /dev/null

echo "==> Засев категорий каталога для всех тенантов..."
$COMPOSE exec -T django python manage.py seed_tenant_categories

# ── 6. Все остальные сервисы ──────────────────────────────────────────────────
echo "==> Запуск celery, frontend, nginx..."
$COMPOSE up -d --build

echo "==> Перезапуск nginx для обновления upstream DNS..."
$COMPOSE restart nginx

echo ""
echo "  Деплой завершён:"
echo "    Backend:  https://$(hostname -f 2>/dev/null || echo localhost)"
echo "    Swagger:  https://$(hostname -f 2>/dev/null || echo localhost)/api/docs/"
echo ""
$COMPOSE ps

echo ""
echo "==> Последние логи Docker Compose (tail=${LOG_TAIL}):"
# Без -f: печатаем последние строки и выходим. Раньше здесь висел `logs -f`,
# и каждый (особенно фоновый) запуск deploy.sh оставлял незавершённый процесс.
$COMPOSE logs --tail="$LOG_TAIL" "${COMPOSE_LOG_SERVICES[@]}"
