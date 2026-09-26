# ADR-0028: Operational context vs durable memory — явное разделение

Статус: Принято (v0.4)

## Контекст

Harness'у для продолжения работы нужны две разные вещи: авторитетное
ТЕКУЩЕЕ состояние (кто держит claim, статус run, gates) и НАКОПЛЕННОЕ знание
(прошлые findings, решения, связанные факты). Смешение делает устаревший
воспоминаемый факт («задачей владеет A») неотличимым от текущей истины
(«задачей владеет B»).

## Решение

`POST /api/v1/context` возвращает две явно разделённые половины:

- `operational` — authoritative-состояние из транзакции этого запроса
  (bootstrap-контекст + фокус task/run: task, claim, runs, artifacts,
  approvals). Всегда приоритетно; memory никогда его не переопределяет.
- `memory` — ContextPack внешнего Memory Service verbatim (sections,
  sources, budget, `trace_id` → `memoryTraceId` для отладки «почему агент
  получил этот контекст»). Eventually consistent по определению.

Ключевые механики:

- Операционный снапшот передаётся провайдеру как `ephemeral_context`:
  Memory-контракт компилирует его в секцию `current` пакета, но НЕ
  персистит (проверено контрактным тестом: контент отсутствует и в
  observations, и в сохранённом trace). Никакого синхронного «flush событий
  перед контекстом» — durable-ингест идёт только асинхронно через adapter.
- Деградация: provider disabled/недоступен/таймаут → HTTP 200,
  `memory: null`, `memoryStatus: disabled|unavailable|timeout`, warnings;
  операционная половина всегда полна. Таймаут конфигурируем
  (`CP_CONTEXT_TIMEOUT_SECONDS`, default 3s — memory build_context p50≈43ms
  на 10k наблюдений, огромный запас).
- Freshness: `freshness.currentCursor` (фронтир журнала tenant'а),
  `memoryCursor` (позиция адаптера), `memoryLagEvents` (per-tenant счётчик,
  cap 1000 + флаг capped — честная метрика без дорогих полных COUNT).
- Scopes/anchors: сервер резолвит и авторизует ДО вызова провайдера
  (tenant-принадлежность task/run/workspace; workspace-предки добавляются,
  sibling-поддеревья — нет). Token budget клиента клампится серверным
  потолком; budgeting сам — в Memory (не дублируем компилятор).
- Кэша нет намеренно: операционная половина слишком волатильна, memory уже
  быстра; кэш — только после benchmark-доказательства (§88).

## Последствия

Новый harness-процесс без прошлого разговора восстанавливает работу одним
вызовом (E2E continuity), при этом устаревшая память структурно не способна
маскироваться под текущее состояние.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- route: "POST /context"
  repo: control-plane
- grep: {path: src/control_plane/application/queries/context.py, pattern: '"ephemeral_context": ephemeral'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/context.py, pattern: '"memoryStatus": "disabled"'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/context.py, pattern: '"memoryLagEvents"'}
  repo: control-plane
- grep: {path: src/control_plane/config.py, pattern: 'context_timeout_seconds: float = 3\.0'}
  repo: control-plane
- grep: {path: tests/contract/test_memory_contract.py, pattern: 'def test_ephemeral_context_is_compiled_but_never_persisted'}
  repo: control-plane
```
