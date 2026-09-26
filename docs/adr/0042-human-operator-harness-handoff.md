# ADR-0042: Human Operator Harness и server-authoritative handoff

Статус: Принято (v0.6)

## Контекст

Harness Protocol уже позволяет одному Principal продолжать работу после
рестарта, но общий MCP adapter был описан как Claude Code-specific, а
переключение между двумя интерактивными harness не имело одной атомарной
команды. Для pilot человек должен управлять Tasks из Codex и Claude Code без
создания отдельного agent Principal и без переноса transcript/process state.

## Решение

- Codex и Claude Code используют один product-neutral `control-plane-mcp` и
  один human Principal. `harness.type`, `harness.version` и `clientName`
  задаются environment процесса; client version берётся из metadata
  установленного package.
- Session получает серверное наблюдаемое поле `controlLevel`. Для human
  Principal это `human_operated`, для agent/service — `connected`. Клиент не
  может передать поле, а authorization никогда его не читает.
- Пользовательское намерение остаётся корневой Task. Декомпозиция использует
  существующую relation `parent`; создание child с `parentTask` выполняется в
  одной транзакции с relation.
- `POST /runs/{runId}:handoff` с `Idempotency-Key` атомарно блокирует Task,
  Claim и Run, проверяет ownership/lease/fencing, пишет checkpoint `handoff`,
  suspend'ит Run, освобождает Claim, возвращает Task в `todo` и публикует
  события/outbox.
- Следующий harness создаёт новый Session, Claim и Run. Контекст берётся из
  сервера; checkpoint/artifacts/events переносят только явные operational
  facts и evidence, не transcript, hidden reasoning или process memory.
- Human confirmation остаётся политикой operator harness. MCP descriptions и
  annotations помогают UI, но серверные permissions, tenant scope, optimistic
  version и fencing остаются единственной enforcement boundary.

## Последствия

- Два vendor harness различимы в аудите и эквивалентны по правам.
- Handoff устойчив к ambiguous response: replay того же idempotency key
  возвращает тот же response и не создаёт второй checkpoint или fencing epoch.
- `controlLevel` нельзя использовать как trust signal; legacy sessions после
  миграции получают нейтральное `connected`.
- Абсолютные local paths, credentials и chat history запрещены в handoff
  payload; сервер отклоняет известные формы секретов и machine-local paths.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- migration: 72ef8bc31a06
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: "control_level IN \\('managed', 'connected', 'human_operated'\\)"}
  repo: control-plane
- route: "POST /runs/{run_id}:handoff"
  repo: control-plane
- grep: {path: src/control_plane/application/commands/runs.py, pattern: 'code="unsafe_handoff_payload"'}
  repo: control-plane
- grep: {path: src/control_plane/api/v1/tasks.py, pattern: 'payload\.parent_task is not None'}
  repo: control-plane
- grep: {path: tests/integration/test_operator_harness_v06.py, pattern: 'def test_control_level_is_server_derived_and_audited'}
  repo: control-plane
```
