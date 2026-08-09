# DEV — Шпаргалка по командам

## Быстрый старт

```bash
./dev.sh          # Запустить ВСЁ: Docker (бэкенд) + Next.js (фронтенд)
```

`dev.sh` жёстко фиксирует Compose file/project этого репозитория, не выполняет
host-wide `docker prune` и не завершает процессы по номеру порта. После остановки
собственного старого стенда он проверяет порты 3000/8000/5432/6379 и безопасно
завершается при внешнем конфликте. `--clean` удаляет только локальный
`frontend/.next`; Docker volumes и кеши других проектов не затрагиваются.
`Ctrl+C` останавливает frontend и контейнеры только этого dev-проекта.

Адреса после запуска:
- **Frontend**: http://localhost:3000
- **Backend API**: http://localhost:8000/api/v1/
- **Swagger**: http://localhost:8000/api/docs/
- **Admin**: http://localhost:8000/admin/

---

## Docker / Бэкенд

Все ручные Compose-команды ниже используют фиксированные project name, project
directory и Compose file. Из корня репозитория один раз задайте массив в текущем
Bash-сеансе; если он не задан, команда безопасно завершится вместо обращения к
неявно выбранному проекту:

```bash
DEV_ROOT="$(pwd -P)"
COMPOSE=(
  docker compose
  --project-name saas_poster
  --project-directory "$DEV_ROOT"
  -f "$DEV_ROOT/docker-compose.yml"
)
```

```bash
make bootstrap      # Первый запуск: миграции/seed/Beat до application-сервисов
make up             # Режим A: все сервисы, включая frontend, в Docker
make down           # Остановить все сервисы
make restart        # Перезапустить все сервисы (НЕ перечитывает .env)
make restart-django # Перезапустить только Django (НЕ перечитывает .env)
make rebuild        # Пересобрать образы и перезапустить (после изменений Dockerfile/requirements)
make shell          # bash внутри контейнера Django

# ⚠️ После изменения .env нужно пересоздать контейнер (не restart!):
make down && make up
```

### Логи

```bash
make logs         # Все сервисы (live)
make logs-django  # Только Django
make logs-celery  # Только Celery worker + beat

"${COMPOSE[@]}" logs -f db       # Логи PostgreSQL
"${COMPOSE[@]}" logs -f redis    # Единый Redis локального dev-контура
```

---

## Фронтенд (Next.js)

`make up` уже занимает порт 3000 containerized frontend-ом. Для локального Next.js
используйте вместо него `./dev.sh`; не смешивайте два режима. `dev.sh` и
`make frontend` по умолчанию направляют браузерный API на
`http://localhost:8000`, сохраняя явный `NEXT_PUBLIC_API_URL` override.
`make frontend` предназначен только для случая, когда Compose frontend заведомо
остановлен.

```bash
make frontend              # Запустить Next.js dev server отдельно
cd frontend && NEXT_PUBLIC_API_URL="${NEXT_PUBLIC_API_URL:-http://localhost:8000}" npm run dev

cd frontend && npm run build   # Сборка production bundle
cd frontend && npm run start   # Запустить production сервер (после build)
cd frontend && npm run lint    # Проверка ESLint
cd frontend && npm run typecheck # Проверка TypeScript
cd frontend && npm run test:unit # Критичные auth/session/billing контракты
```

---

## База данных / Миграции

```bash
make migrate          # Применить все миграции
make migrations       # Создать новые миграции (makemigrations)

# Конкретное приложение:
"${COMPOSE[@]}" exec django python manage.py makemigrations APP_LABEL
"${COMPOSE[@]}" exec django python manage.py migrate APP_LABEL

# Откат миграции:
"${COMPOSE[@]}" exec django python manage.py migrate APP_LABEL MIGRATION_NAME

# Показать все миграции и их статус:
"${COMPOSE[@]}" exec django python manage.py showmigrations
```

Reverse migration допустима только в локальном dev-контуре после проверки плана
и сохранности нужных данных: операция может удалить столбцы или строки. В
production автоматический откат схемы запрещён — используется forward recovery
по [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md#7-ошибка-и-восстановление).

---

## Тесты

```bash
make test                        # Все тесты с coverage
"${COMPOSE[@]}" exec django pytest apps/APP_LABEL/tests/ -v
"${COMPOSE[@]}" exec django pytest -k "test_name" -v
"${COMPOSE[@]}" exec django pytest --cov=apps --cov-report=html

# Проверки, которым не нужен Docker daemon:
make runtime-check
cd frontend && npm run test:unit && npm run typecheck && npm run lint
```

---

## Линтинг и типизация

```bash
make lint                        # flake8 по всему репозиторию
make typecheck-backend           # Честный инкрементальный scope из mypy.ini
```

Полный `mypy apps/` пока не является зелёным gate: большая часть legacy Django
моделей ещё не имеет корректного typing baseline. `mypy.ini` намеренно проверяет
только уже чистые модули и должен расширяться вместе с исправлениями; глобальный
`ignore_errors` запрещён.

---

## OpenAPI

```bash
# Строгая проверка схемы: команда завершается ошибкой при любом warning/error
"${COMPOSE[@]}" exec django python manage.py spectacular \
  --file /tmp/openapi-schema.yml --validate --fail-on-warn
```

Эту же проверку выполняет CI. При изменении API явно описывайте request/response,
параметры и уникальный `operation_id`; не подавляйте предупреждения генератора.

---

## Django Management Commands

```bash
make seed                        # Заполнить тарифные планы (seed_plans)
make setup-periodic
make superuser
"${COMPOSE[@]}" exec django python manage.py collectstatic
make shell
```

---

## Celery

```bash
# Запустить задачу вручную через Django shell:
"${COMPOSE[@]}" exec django python manage.py shell -c "
from apps.products.tasks import import_from_datasource
import_from_datasource.delay(1)
"

# Проверка доступных Celery worker-ов:
"${COMPOSE[@]}" exec celery_worker celery -A config inspect ping
```

Очистка Redis через `FLUSHDB` намеренно не приведена: локальный dev-контур
использует один Redis для cache/очередей, а production — раздельные cache и
durable broker процессы. Очистка способна уничтожить queued/ETA tasks, результаты
и coordination locks. Делайте purge только по отдельной incident-процедуре после
drain и фиксации затронутых очередей.

---

## Резервное копирование БД

```bash
make backup        # Зашифрованный и подписанный production backup в private S3
make backup-check  # Проверить подпись, manifest и свежесть последнего backup
```

Обе команды требуют production-конфигурацию `.backup.env`; это не локальный gzip.
Восстановление выполняется только в отдельную пустую БД через изолированный
restore-контур. Никогда не направляйте сырой dump напрямую в production. Полная
процедура, обязательные подтверждения и restore drill описаны в
[`docs/BACKUP_RESTORE.md`](docs/BACKUP_RESTORE.md).

---

## Безопасная пересборка текущего проекта

```bash
"${COMPOSE[@]}" down --remove-orphans # Остановить только фиксированный проект
"${COMPOSE[@]}" build                 # Пересобрать изменившиеся образы
"${COMPOSE[@]}" up -d                 # Поднять
make migrate                    # Применить миграции
make seed                       # Заполнить начальные данные
make superuser
```

Удаление volumes намеренно не входит в шпаргалку: оно уничтожает локальную БД.
Если нужен чистый стенд, сначала создайте проверяемый backup и явно определите,
какие именно project-local volumes допустимо удалить.

---

## Git / Ветки (RULEBOOK)

```bash
git checkout develop
git pull origin develop
git checkout -b feature/BRANCH_NAME   # Новая фича от develop
git checkout -b fix/BRANCH_NAME       # Багфикс

# Коммит (Conventional Commits, описание на русском):
git commit -m "feat(scope): добавить ..."
git commit -m "fix(scope): исправить ..."
git commit -m "refactor(scope): переработать ..."
git commit -m "test(scope): добавить тесты для ..."
git commit -m "docs(scope): обновить ..."

# PR через GitHub CLI:
gh pr create --base develop --title "feat: ..." --body "..."
gh pr merge PR_NUMBER --squash --delete-branch
```

`APP_LABEL`, `MIGRATION_NAME`, `BRANCH_NAME` и `PR_NUMBER` — безопасные явные
placeholders: замените их реальными значениями перед выполнением команды.

`develop` — интеграционная ветка: её `push` запускает CI, но не production deploy.
Production release оформляется отдельным reviewed merge/PR в `main`; только
успешный CI для соответствующего `push` в `main` может запустить защищённый deploy.
