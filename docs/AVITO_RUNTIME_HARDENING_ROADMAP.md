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

Статус: `VERIFIED` 2026-08-28.

Результат: pending-листинг нельзя локально архивировать, удалять или переносить
на другой аккаунт, пока владеющий им preparing/submitted/uncertain run ещё
может создать объявление; legacy pending без точного generation ID работает
fail-closed; API и UI отличают локальную подготовку, подготовку XML, обработку
Avito, retry, ошибку и ручную сверку. Ручная проверка Avito доступна только
после доказанного начала provider-доставки.

Границы: marketplace service/API/serializer и listing UI. Без миграций и P7.

Фактический gate: focused lifecycle/delivery `78 passed`, финальная отдельная
проверка PREPARING-race `14 passed`; Marketplace `874 passed, 2 skipped`;
полный backend `2673 passed, 3 skipped`; frontend typecheck, eslint и unit
`29 passed`; Next production build через Webpack (21 route) — exit 0; flake8,
mypy (693 sources), strict mypy (349 sources),
`makemigrations --check --dry-run` и `git diff --check` — exit 0. Нативный
Turbopack build в локальном sandbox не смог открыть служебный loopback port;
повтор вне sandbox дал тот же OS-level `Operation not permitted`, поэтому
production bundle проверен поддерживаемым `next build --webpack`.

## H2 — восстанавливаемый onboarding новых аккаунтов

Статус: `VERIFIED` 2026-08-28.

Результат: сбой broker после создания аккаунта не превращает успешный commit в
ошибку API; незавершённый onboarding подбирается bounded scanner; exhausted и
lock outcomes видны и повторяемы; same-account credential rotation безопасна;
pre-profile endpoint readiness проверяется явно.

Границы: marketplace onboarding/tasks/views и существующий scheduler. Без
массового sweep старых профилей, новых режимов и миграций.

Результат: API создания аккаунта остаётся успешным при недоступном broker;
потерянная постановка подбирается bounded scanner каждые пять минут; lock,
retry exhaustion и ручная safety-сверка имеют durable tenant-visible статус;
неопределённый POST повторяется только через GET-only reconciliation. Новый
endpoint можно безопасно re-key до первого POST, а подтверждённый endpoint
разрешает только семантически неизменные credentials. Local bridge/capability
проверяются до изменения профиля Avito; мастер подключения различает duplicate
и опасный 409, а UI отдельно показывает тариф Avito и готовность фида MAP.

Фактический gate: focused onboarding/account regressions `71 passed`;
Marketplace `885 passed, 2 skipped`; полный backend `2684 passed, 3 skipped`;
frontend unit `29 passed`, typecheck, eslint и production Webpack build (21
route) — exit 0; flake8, mypy (695 sources), strict mypy (350 sources), OpenAPI
validation, `makemigrations --check --dry-run` и `git diff --check` — exit 0.

## H3 — статистика и плановая сверка

Статус: `VERIFIED` 2026-08-28.

Результат: московская дата используется последовательно; нечисловой внешний ID
не ломает весь аккаунт; inactive tenants не планируются; bounded history
backfill закрывает многодневные пропуски; status scheduler ставит только due
rows, а не весь активный fleet каждые десять минут.

Границы: marketplace stats/status tasks, adapter и scheduler. Без новых таблиц.

Результат: статистика ежедневно восстанавливает bounded 14-day окно по
московской дате и не планирует выключенные tenants/accounts; повреждённый
external ID или отдельный malformed provider item больше не ломает весь
аккаунт, дубли дедуплицируются, counters ограничиваются размером DB-поля.
Worker не ретраит навсегда неверные диапазоны. Account/listing due timestamps,
lease и cooldown теперь ограничивают status polling точными due rows; broker
failure освобождает claim и не блокирует остальные dispatch.

Фактический gate: focused cross-package regressions `46 passed`, финальные
stats/scheduler regressions после metric audit `25 passed`;
Marketplace `894 passed, 2 skipped`; полный backend `2698 passed, 3 skipped`;
flake8, mypy (696 sources), strict mypy (350 sources), OpenAPI validation,
`makemigrations --check --dry-run` и `git diff --check` — exit 0.

## H4 — итоговый gate и удаление roadmap

Статус: `IN_PROGRESS`.

Результат: все узкие и полные проверки зелёные, migration drift отсутствует,
рабочий diff не содержит личные настройки или P7, этот временный файл удалён.
