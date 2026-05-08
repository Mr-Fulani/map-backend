.PHONY: up down shell migrate migrations test lint seed backup

up:
	docker compose up -d

down:
	docker compose down

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

backup:
	docker compose exec db pg_dump -U map_user map_db | gzip > backup_$$(date +%Y%m%d_%H%M%S).sql.gz
