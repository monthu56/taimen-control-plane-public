# ADR-0019. Execution audit (RunAction) отдельно от доменного журнала

Статус: принято (2026-08-11, v0.3)

## Контекст

Enterprise-аудит требует минимального execution trace (какие инструменты/skills
вызывались, чем закончились). Писать каждый tool-call в append-only `events`
превратило бы доменный журнал в telemetry firehose с другой скоростью роста и
другим уровнем доверия к содержимому.

## Решение

Отдельная таблица `run_actions`: `run_id`, `seq` (per-run, уникален),
`action`, `status` (started/completed/failed), `skill_id?`,
`external_reference?`, `metadata`, `started_at/finished_at`. Записывает только
живой владелец run'а (полный claim-gate под локом задачи) — зомби не
загрязняет аудит. Два стиля записи: одним вызовом (готовое действие) или
двухфазно (`POST /runs/{id}/actions` со status=started → `:finish`).

Бюджеты исполнения (`max_actions`, `max_duration_seconds` на run) enforce'ятся
именно здесь: запись сверх бюджета → `409 budget_exceeded`; финализация run
бюджетом не блокируется.

Не сохраняются: секреты, скрытый chain-of-thought, полные inputs/outputs —
только ссылки (`external_reference`) и небольшие метаданные.

Доменные события (`run.started`, `run.suspended`, `approval.requested`, ...)
остаются в `events`; checkpoint пишет лёгкое событие `run.checkpointed`
(без data) — это доменный факт «состояние сохранено», полезный подписчикам.

## Обоснование

Разделение по назначению: `events` — доменная история для replay и realtime,
`run_actions` — операционная телеметрия для аудита. Разные объёмы, разные
читатели, разные retention-политики в будущем.

## Последствия

- `GET /runs/{id}/actions` — единственный читатель; в событийный поток
  действия не попадают.
- `seq` выделяется под локом run — стабильный порядок для аудита.
- Server-side бюджет — enforce того, что сервер способен проверить; остальное
  (реальное время работы) — на совести harness, как и задокументировано.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'UniqueConstraint\("run_id", "seq", name="uq_run_actions_run_seq"\)'}
  repo: control-plane
- route: "POST /runs/{run_id}/actions"
  repo: control-plane
- route: "POST /runs/{run_id}/actions/{action_id}:finish"
  repo: control-plane
- grep: {path: src/control_plane/application/commands/execution.py, pattern: '"budget_exceeded"'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/execution.py, pattern: 'event_type="run\.checkpointed"'}
  repo: control-plane
```
