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

API-клиент находится в `src/lib/api.ts`. JWT access и refresh токены выдаются
через `/api/v1/auth/token/`; access token обновляется автоматически после 401.

В production `NEXT_PUBLIC_API_URL` следует оставить пустым. Запросы пойдут на
same-origin `/api`, а Nginx направит их в Django. Для отдельного локального
frontend укажите `http://localhost:8000`.

Основные страницы dashboard находятся в `src/app/dashboard/`, общие компоненты —
в `src/components/`, API-функции и auth context — в `src/lib/`.
