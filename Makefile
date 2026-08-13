.PHONY: bootstrap up down dev frontend frontend-test rebuild restart restart-django logs logs-django logs-celery shell migrate migrations test lint typecheck-backend runtime-check seed setup-periodic superuser backup backup-check telegram-poll

PYTHON ?= python3
COMPOSE := docker compose --project-name saas_poster --project-directory "$(CURDIR)" -f "$(CURDIR)/docker-compose.yml"

bootstrap:
	$(COMPOSE) up -d --wait --wait-timeout 120 db redis
	$(COMPOSE) run --rm --no-deps --build django python manage.py migrate --noinput
	$(COMPOSE) run --rm --no-deps django python manage.py seed_plans
	$(COMPOSE) run --rm --no-deps django python manage.py setup_periodic_tasks
	$(COMPOSE) up -d --build

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

# Запустить бэкенд + фронтенд одной командой
dev:
	./dev.sh

# Только Next.js dev server
frontend:
	cd frontend && NEXT_PUBLIC_API_URL="$${NEXT_PUBLIC_API_URL:-http://localhost:8000}" npm run dev

# Критичные frontend auth/session/billing тесты без Docker daemon.
frontend-test:
	cd frontend && npm run test:unit

# Пересобрать Docker-образы и перезапустить
rebuild:
	$(COMPOSE) down
	$(COMPOSE) build --no-cache
	$(COMPOSE) up -d

# Перезапуск всех Docker-сервисов (без пересборки)
restart:
	$(COMPOSE) restart

# Перезапуск только Django без перечитывания .env.
restart-django:
	$(COMPOSE) restart django

# Логи всех сервисов (live)
logs:
	$(COMPOSE) logs -f

# Логи только Django
logs-django:
	$(COMPOSE) logs -f django

# Логи обоих Celery workers и Beat.
logs-celery:
	$(COMPOSE) logs -f celery_worker celery_worker_images celery_beat

shell:
	$(COMPOSE) exec django bash

migrate:
	$(COMPOSE) exec django python manage.py migrate

migrations:
	$(COMPOSE) exec django python manage.py makemigrations

test:
	$(COMPOSE) exec django pytest --cov=apps --cov-report=term-missing

lint:
	$(COMPOSE) exec django flake8 .

# Application-wide mypy baseline; scope перечислен в mypy.ini.
typecheck-backend:
	$(COMPOSE) exec django mypy

# Статические runtime-контракты без обращения к Docker daemon.
runtime-check:
	$(PYTHON) -m pytest tests/test_runtime_contract.py tests/test_healthchecks.py tests/test_deploy_contract.py

seed:
	$(COMPOSE) exec django python manage.py seed_plans

setup-periodic:
	$(COMPOSE) exec django python manage.py setup_periodic_tasks

superuser:
	$(COMPOSE) exec django python manage.py createsuperuser

# Telegram long polling для локальной разработки (вместо webhook)
telegram-poll:
	$(COMPOSE) exec django python manage.py telegram_poll

backup:
	./scripts/production_backup.sh

backup-check:
	./scripts/production_backup_check.sh
