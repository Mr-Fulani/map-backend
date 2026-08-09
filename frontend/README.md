# MAP Dashboard

Next.js 14 dashboard для Marketplace Automation Platform.

## Локальный запуск

```bash
npm ci
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

Frontend доступен на `http://localhost:3000`, Django API — на
`http://localhost:8000/api/v1/`.

## Проверки

```bash
npm run lint
npm run build
```

## API и авторизация

API-клиент находится в `src/lib/api.ts`. Browser login и refresh выполняются
через CSRF-защищённые `/api/v1/auth/browser/*`: refresh остаётся только в
HttpOnly cookie, а access token — только в памяти вкладки. Поле
`browser_session_id` не является credential: это стабильный идентификатор
refresh-цепочки для согласования сессии между вкладками. Он не даёт повторить
старый запрос после смены пользователя. `/api/v1/auth/token/*` предназначены
для header-based клиентов.

В production `NEXT_PUBLIC_API_URL` следует оставить пустым. Запросы пойдут на
same-origin `/api`, а Nginx направит их в Django. Для отдельного локального
frontend укажите `http://localhost:8000`.

Основные страницы dashboard находятся в `src/app/dashboard/`, общие компоненты —
в `src/components/`, API-функции и auth context — в `src/lib/`.
