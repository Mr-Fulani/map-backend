# DEV — Шпаргалка по командам

## Быстрый старт

```bash
./dev.sh          # Запустить ВСЁ: Docker (бэкенд) + Next.js (фронтенд)
```

Адреса после запуска:
- **Frontend**: http://localhost:3000
- **Backend API**: http://localhost:8000/api/v1/
- **Swagger**: http://localhost:8000/api/docs/
- **Admin**: http://localhost:8000/admin/

---

## Docker / Бэкенд

```bash
make up             # Поднять все Docker-сервисы (фоново)
make down           # Остановить все сервисы
make restart        # Перезапустить все сервисы (НЕ перечитывает .env)
make restart-django # Перезапустить только Django (НЕ перечитывает .env)
make rebuild        # Пересобрать образы и перезапустить (после изменений Dockerfile/requirements)
make shell          # bash внутри контейнера Django

# ⚠️ После изменения .env нужно пересоздать контейнер (не restart!):
docker compose up -d django   # пересоздаёт только Django с новыми переменными
docker compose up -d          # пересоздаёт все сервисы
```

### Логи

```bash
make logs         # Все сервисы (live)
make logs-django  # Только Django
make logs-celery  # Только Celery worker + beat

docker compose logs -f db       # Логи PostgreSQL
docker compose logs -f redis    # Логи Redis
```

---

## Фронтенд (Next.js)

```bash
make frontend              # Запустить Next.js dev server отдельно
cd frontend && npm run dev # То же самое напрямую

cd frontend && npm run build   # Сборка production bundle
cd frontend && npm run start   # Запустить production сервер (после build)
cd frontend && npm run lint    # Проверка ESLint
```

---

## База данных / Миграции

```bash
make migrate          # Применить все миграции
make migrations       # Создать новые миграции (makemigrations)

# Конкретное приложение:
docker compose exec django python manage.py makemigrations <app>
docker compose exec django python manage.py migrate <app>

# Откат миграции:
docker compose exec django python manage.py migrate <app> <номер_миграции>

# Показать все миграции и их статус:
docker compose exec django python manage.py showmigrations
```

---

## Тесты

```bash
make test                        # Все тесты с coverage
docker compose exec django pytest apps/<app>/tests/ -v   # Тесты одного приложения
docker compose exec django pytest -k "test_name" -v      # По имени теста
docker compose exec django pytest --cov=apps --cov-report=html  # HTML отчёт
```

---

## Линтинг и типизация

```bash
make lint                        # flake8 + mypy
docker compose exec django flake8 apps/
docker compose exec django mypy apps/
```

---

## OpenAPI

```bash
# Строгая проверка схемы: команда завершается ошибкой при любом warning/error
python manage.py spectacular --file /tmp/openapi-schema.yml --validate --fail-on-warn
```

Эту же проверку выполняет CI. При изменении API явно описывайте request/response,
параметры и уникальный `operation_id`; не подавляйте предупреждения генератора.

---

## Django Management Commands

```bash
make seed                        # Заполнить тарифные планы (seed_plans)
docker compose exec django python manage.py setup_periodic_tasks
docker compose exec django python manage.py createsuperuser
docker compose exec django python manage.py collectstatic
docker compose exec django python manage.py shell         # Django shell
```

---

## Celery

```bash
# Запустить задачу вручную через Django shell:
docker compose exec django python manage.py shell -c "
from apps.products.tasks import import_from_datasource
import_from_datasource.delay(1)
"

# Проверка доступных Celery worker-ов:
docker compose exec celery_worker celery -A config inspect ping

# Очистить очередь Redis:
docker compose exec redis redis-cli FLUSHDB
```

---

## Резервное копирование БД

```bash
make backup    # Дамп БД в backup_YYYYMMDD_HHMMSS.sql.gz

# Восстановление:
zcat backup_YYYYMMDD_HHMMSS.sql.gz | docker compose exec -T db psql -U map_user map_db
```

---

## Полный перезапуск (с нуля)

```bash
docker compose down -v          # Остановить + удалить volumes (ДАННЫЕ УДАЛЯТСЯ)
docker compose build --no-cache # Пересобрать образы
docker compose up -d            # Поднять
make migrate                    # Применить миграции
make seed                       # Заполнить начальные данные
docker compose exec django python manage.py createsuperuser
```

---

## Git / Ветки (RULEBOOK)

```bash
git checkout develop
git pull origin develop
git checkout -b feature/<name>   # Новая фича от develop
git checkout -b fix/<name>       # Багфикс

# Коммит (Conventional Commits, описание на русском):
git commit -m "feat(scope): добавить ..."
git commit -m "fix(scope): исправить ..."
git commit -m "refactor(scope): переработать ..."
git commit -m "test(scope): добавить тесты для ..."
git commit -m "docs(scope): обновить ..."

# PR через GitHub CLI:
gh pr create --base develop --title "feat: ..." --body "..."
gh pr merge <номер> --squash --delete-branch
```
