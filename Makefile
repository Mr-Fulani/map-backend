.PHONY: up down dev frontend rebuild restart restart-django logs logs-django logs-celery shell migrate migrations test lint seed backup backup-check telegram-poll

up:
	docker image prune -f > /dev/null 2>&1 || true
	docker builder prune -f --filter "until=168h" > /dev/null 2>&1 || true
	docker compose up -d

down:
	docker compose down

# Запустить бэкенд + фронтенд одной командой
dev:
	./dev.sh

# Только Next.js dev server
frontend:
	cd frontend && npm run dev

# Пересобрать Docker-образы и перезапустить
rebuild:
	docker compose down
	docker compose build --no-cache
	docker compose up -d

# Перезапуск всех Docker-сервисов (без пересборки)
restart:
	docker compose restart

# Перезапуск только Django (например, после изменения .env)
restart-django:
	docker compose restart django

# Логи всех сервисов (live)
logs:
	docker compose logs -f

# Логи только Django
logs-django:
	docker compose logs -f django

# Логи Celery (worker + beat)
logs-celery:
	docker compose logs -f celery_worker celery_beat

shell:
	docker compose exec django bash

migrate:
	docker compose exec django python manage.py migrate

migrations:
	docker compose exec django python manage.py makemigrations

test:
	docker compose exec django pytest --cov=apps --cov-report=term-missing

lint:
	docker compose exec django flake8 apps/
	docker compose exec django mypy apps/

seed:
	docker compose exec django python manage.py seed_plans

# Telegram long polling для локальной разработки (вместо webhook)
telegram-poll:
	docker compose exec django python manage.py telegram_poll

backup:
	./scripts/production_backup.sh

backup-check:
	./scripts/production_backup_check.sh
