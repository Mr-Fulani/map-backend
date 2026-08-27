# Временный roadmap Avito runtime hardening

Создан: 2026-08-28.

Этот файл существует только на время выполнения подтверждённого аудита. После
закрытия всех пакетов и финального gate он удаляется. P7, object deletion, GC,
retention и `0039` в этот roadmap не входят.

## H0 — восстановить публикацию

Статус: `VERIFIED` 2026-08-28.

Результат: любой проверенный переход `queued -> pending` создаёт новую точную
feed revision; прямое снятие `active -> archiving` не теряет removal intent;
старое предупреждение очищается при approve; зависший pending без run
автоматически обнаруживается и безопасно переотправляется.

Границы: marketplace tasks/services и их тесты. Без миграций, новых режимов,
очередей и внешних вызовов.

Gate: production-like dual-write/durable regressions, feed-intent tests,
marketplace tests, полный backend, flake8, mypy, migration drift и diff check.

Фактический gate: `66 passed`; Marketplace `860 passed, 2 skipped`; полный
backend `2659 passed, 3 skipped`; flake8, mypy (691 sources), strict mypy
(348 sources), `makemigrations --check --dry-run` и `git diff --check` — exit 0.

## H1 — безопасные lifecycle-переходы и честный UI

Статус: `IN_PROGRESS`.

Результат: pending-листинг нельзя локально архивировать, удалять или переносить
на другой аккаунт, пока submitted/uncertain run ещё может создать объявление;
UI отличает локальную подготовку от реально отправленного фида.

Границы: marketplace service/API/serializer и listing UI. Без миграций и P7.

## H2 — восстанавливаемый onboarding новых аккаунтов

Статус: `NOT_STARTED`.

Результат: сбой broker после создания аккаунта не превращает успешный commit в
ошибку API; незавершённый onboarding подбирается bounded scanner; exhausted и
lock outcomes видны и повторяемы; same-account credential rotation безопасна;
pre-profile endpoint readiness проверяется явно.

Границы: marketplace onboarding/tasks/views и существующий scheduler. Без
массового sweep старых профилей, новых режимов и миграций.

## H3 — статистика и плановая сверка

Статус: `NOT_STARTED`.

Результат: московская дата используется последовательно; нечисловой внешний ID
не ломает весь аккаунт; inactive tenants не планируются; bounded history
backfill закрывает многодневные пропуски; status scheduler ставит только due
rows, а не весь активный fleet каждые десять минут.

Границы: marketplace stats/status tasks, adapter и scheduler. Без новых таблиц.

## H4 — итоговый gate и удаление roadmap

Статус: `NOT_STARTED`.

Результат: все узкие и полные проверки зелёные, migration drift отсутствует,
рабочий diff не содержит личные настройки или P7, этот временный файл удалён.
