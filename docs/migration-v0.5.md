# Миграция v0.4 → v0.5

Документ для инженера, который обновляет работающую установку. Он покрывает
схему, breaking-изменения клиентских инструментов и эксплуатационные шаги.

## Порядок обновления

1. Остановить `context-adapter` (он единственный держит singleton advisory
   lock и пишет курсоры).
2. `alembic upgrade head` — одна ревизия `1adf50721f1e`.
3. Запустить API и worker, проверить `GET /health/ready` (при отставшей
   ревизии он отдаёт `503 migrations_pending`).
4. Запустить `context-adapter`.

Даунгрейд `alembic downgrade 2cb05920015d` поддержан и проверен тестом
`tests/integration/test_migration_v05.py`.

## Что делает миграция

| Область | Действие | Ручные шаги |
|---|---|---|
| Workspace Types | создаёт `workspace_types`, системный тип `generic` на каждый Tenant, backfill `workspaces.type_id` | нет |
| Project Model | создаёт `project_templates`, `project_profiles`, `project_config_revisions`, `external_references` и триггеры иммутабельности | нет |
| Trace id | добавляет nullable `events.trace_run_id` | нет |
| Курсоры доставки | PK `event_consumer_cursors` → `(name, tenant_id)`, разворачивает глобальную позицию в позицию каждого Tenant | нет |
| Retention | создаёт `event_archive`, `event_journal_floor`, ослабляет append-only-триггер ровно для операции архивации | нет |

Преобразование курсора **replay-safe**: глобальная позиция G означала «всё до
G доставлено», и каждая per-tenant строка получает ту же G. Повторной доставки
всего журнала не происходит. Downgrade сворачивает строки в одну по минимуму —
консервативно (часть событий доставится повторно), без потерь.

Индексы строятся не `CONCURRENTLY`, а backfill `workspaces.type_id` трогает
каждую строку: планируйте maintenance window пропорционально размеру дерева.

## Breaking: закрытие compatibility window (ADR-0040)

Серверный HTTP API **не затронут**. Ломаются только клиентские точки входа,
которые несли историческое продуктовое кодовое имя.

| Удалено | Замена |
|---|---|
| пакет `taimen_client` (и подмодули `taimen_client.client` и т.д.) | `control_plane_client` |
| `TaimenClient`, `TaimenError` | `ControlPlaneClient`, `ControlPlaneError` |
| console-scripts `taimen`, `taimen-mcp`, `taimen-agent` | `control-plane`, `control-plane-mcp`, `control-plane-agent` |
| `TAIMEN_API_KEY` | `CONTROL_PLANE_API_KEY` |
| `TAIMEN_SERVER` | `CONTROL_PLANE_SERVER` |
| `TAIMEN_NO_KEYCHAIN` | `CONTROL_PLANE_NO_KEYCHAIN` |
| `TAIMEN_AGENT_ADAPTER` | `CONTROL_PLANE_AGENT_ADAPTER` |
| `TAIMEN_AGENT_WORKSPACE` | `CONTROL_PLANE_AGENT_WORKSPACE` |
| `TAIMEN_AGENT_POLL` | `CONTROL_PLANE_AGENT_POLL` |
| Keychain `ai.taimen.api-key` | `control-plane.api-key` |
| `~/.config/taimen/credentials.json` | `~/.config/control-plane/credentials.json` |
| `.taimen/config.json` | `.control-plane/config.json` |
| `protocol.legacyNames` в bootstrap-контексте | — |

Тихого отказа нет:

- установленная legacy env-переменная (при незаданной новой) — CLI и
  адаптеры печатают `NAME is no longer read (removed in v0.5); use NEW` и
  выходят с кодом 2;
- найденный `.taimen/config.json` — SDK бросает `LegacyProjectConfigError` с
  путём и подсказкой.

Что делать пользователю:

```bash
# ключ: перелогиниться в нейтральное хранилище
control-plane login --server https://control-plane.example

# привязка проекта
mkdir -p .control-plane && git mv .taimen/config.json .control-plane/config.json
```

**Не** сломано: `protocolVersion: "1"` в сессиях, legacy integer
`?after=<sequence>` и v0.3-кодировка `nextCursor` — это протокольная
совместимость (ADR-0024), а не продуктовое имя.

## Новые permissions

Старые API-ключи новых прав **не получают**. Выдайте явно там, где нужно:

- `projects.read`, `projects.manage`;
- `project_templates.read`, `project_templates.manage`;
- `operations.read`, `operations.manage` (диагностика/redrive/retention).

Ключ с `admin` продолжает иметь полный доступ.

## Эксплуатационные изменения

- `/metrics`: `context_adapter_*` теперь агрегируются по всем Tenant,
  добавлен gauge `context_adapter_parked_tenants`. Per-tenant меток нет
  намеренно (кардинальность).
- Новые настройки: `CP_CONTEXT_TENANT_BATCH_SIZE` (100),
  `CP_CONTEXT_MAX_TENANTS_PER_CYCLE` (20),
  `CP_JOURNAL_RETENTION_MIN_AGE_SECONDS` (30 суток).
- Retention — операторская команда, а не cron: `POST
  /api/v1/operations/journal:archive`, затем `:prune`. Архивация ждёт и
  подтверждения всеми consumer-курсорами, и доставки outbox.
- `X-Run-Id` принимается и эхом возвращается на всех запросах; в логах это
  поле `run_id`.
