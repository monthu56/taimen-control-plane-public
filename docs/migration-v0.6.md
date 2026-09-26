# Миграция Control Plane v0.5 → v0.6

## До обновления

1. Сделайте backup PostgreSQL и остановите API/worker v0.5.
2. Зафиксируйте текущую ревизию `1adf50721f1e` и установите package v0.6.
3. Не добавляйте `controlLevel` в client payload: поле назначает сервер.

## Upgrade

```bash
uv run alembic upgrade 72ef8bc31a06
```

Миграция добавляет `sessions.control_level NOT NULL`, backfill'ит все legacy
rows значением `connected` и создаёт check constraint для `managed`,
`connected`, `human_operated`. Новые human sessions получают
`human_operated` в application command; agent/service — `connected`.

После upgrade запустите API/worker и проверьте `/health/ready`, открытие human
Session и `GET /api/v1/harness/context?sessionId=...`.

## Изменения клиентов

- Общий MCP adapter больше не представляется Claude Code по умолчанию. Для
  Codex/Claude Code задайте `CONTROL_PLANE_HARNESS_TYPE`, version и client name.
- SDK добавляет Task CRUD/list/relation и `prepare_handoff` /
  `continue_after_handoff`.
- Handoff требует `Idempotency-Key`; повторять ambiguous request нужно с тем же
  ключом.

## Downgrade

```bash
uv run alembic downgrade 1adf50721f1e
```

Downgrade удаляет constraint и колонку `control_level`. Handoff-created
checkpoints/runs/events остаются валидными v0.5 данными, но специализированный
endpoint недоступен после отката. Перед downgrade остановите v0.6 clients.
