# TASK-000003: Durable Active Turn Control — SPEC

Статус: completed; merged into `main`

Target: `control-plane`

Источник задачи: `TASK-000003`

Референс: внешний исследовательский артефакт Hermes Agent HRS-4

## 1. Проблема и граница

В v0.6 Run имеет только один кооперативный сигнал `cancelRequestedAt`. Он не
различает новое намерение, коррекцию, отмену model inference и жёсткую отмену;
не имеет durable очереди, causal position, lifecycle acknowledgement и
восстановления после restart.

Control Plane не исполняет model/tool loop. Он принимает, упорядочивает,
авторизует и журналирует управляющие сообщения. Harness применяет их на
объявленной safe boundary. Tasks, Claims, Runs, permissions, fencing, audit и
terminal state остаются server-authoritative.

Hermes подтверждает полезность различия interruptible model call и tool batch:
model response можно отбросить без добавления partial response в history, но
уже завершённые tool results нельзя переписать. Мы переносим семантику, а не
Hermes state, class или dependency.

## 2. Пользователи и use cases

- Human/operator отправляет correction работающему Run.
- Orchestrator ставит следующее намерение после terminal boundary turn.
- Harness подтверждает, где и как применил сообщение.
- Operator просит кооперативную остановку и затем видит acknowledgement.
- Principal с `claims.manage` немедленно останавливает Run и отзывает Claim.
- Harness после restart читает неприменённые сообщения из PostgreSQL.
- Parent Run при `force_cancel` server-authoritatively останавливает активные
  дочерние Runs, связанные через `spawned_by`.

## 3. Операции

| Operation | Серверная семантика | Harness semantics |
|---|---|---|
| `queue` | Durable intent после текущего turn | применить на `turn_boundary` |
| `steer` | Durable correction без отмены action | применить после текущего tool batch |
| `redirect` | Durable замена текущего model decision | отменить только model request; во время tools применять как `steer` |
| `request_cancel` | Кооперативный сигнал; после acknowledgement новые Run actions запрещены | остановиться на ближайшей safe boundary и финализировать Run |
| `force_cancel` | Атомарно terminal `cancelled`, release Claim, запрет дальнейших authoritative writes; cascade child Runs | process kill остаётся обязанностью runtime |

## 4. Durable state

Новая append-oriented таблица `run_control_messages`:

- `id`, `tenant_id`, `run_id`, `task_id`;
- `seq`: gap-free порядок внутри Run;
- `operation`: `queue|steer|redirect|request_cancel|force_cancel`;
- `status`: `accepted|applied|rejected|superseded`;
- `causal_position`: opaque bounded marker вызывающей стороны;
- `directive`: bounded intentional instruction для `queue|steer|redirect`;
- `reason`: bounded explanation для cancellation/rejection;
- `idempotency_key`: значение обязательного HTTP `Idempotency-Key`;
- `requested_by_principal_id`, `acknowledged_by_principal_id`;
- `safe_boundary`, `request_id`, `correlation_id`, `causation_id`;
- `version`, `accepted_at`, `resolved_at`.

Инварианты БД:

- unique `(run_id, seq)`;
- unique `(run_id, idempotency_key)`;
- check operation/status/version/seq;
- terminal status требует `resolved_at`;
- `accepted` не имеет `resolved_at`;
- индексы `(tenant_id, run_id, seq)` и unresolved rows.

Directive не является transcript или raw prompt. API ограничивает размер и не
пишет directive/reason в domain event payload. Credentials, tool payloads,
hidden reasoning и абсолютные локальные пути в Control Message не допускаются.

## 5. HTTP API (`/api/v1`)

### `POST /runs/{runId}/control-messages`

Требует `Idempotency-Key`. Для `queue|steer|redirect|request_cancel` требуется
`tasks.write`; для `force_cancel` — `claims.manage`.

```json
{
  "operation": "steer",
  "causalPosition": "turn:17/tool-batch:2",
  "directive": "Сначала проверь миграционный roundtrip",
  "reason": "human correction",
  "expectedRunVersion": 4
}
```

Ответ `201`, `Location: /api/v1/runs/{runId}/control-messages/{id}`:

```json
{
  "controlMessage": {
    "id": "...",
    "runId": "...",
    "seq": 3,
    "operation": "steer",
    "status": "accepted",
    "causalPosition": "turn:17/tool-batch:2",
    "directive": "Сначала проверь миграционный roundtrip",
    "reason": "human correction",
    "safeBoundary": null,
    "version": 1,
    "acceptedAt": "...",
    "resolvedAt": null
  },
  "runVersion": 5
}
```

Доменная и HTTP idempotency действуют совместно: повтор того же key и тела
возвращает тот же результат; key с другим телом — `409
idempotency_key_reused`.

### `GET /runs/{runId}/control-messages?cursor=&limit=`

Требует `tasks.read`. Порядок строго по `seq`. `cursor` непрозрачен (`rc1_...`),
привязан к Run; `limit` default 50, max 200. Ответ:

```json
{"items": [], "nextCursor": "rc1_...", "hasMore": false}
```

Фильтра status нет: иначе продвижение cursor могло бы навсегда скрыть ранее
неразрешённое сообщение.

### `POST /runs/{runId}/control-messages/{messageId}:acknowledge`

Только holder живого Claim. Claim/fencing и optimistic versions передаются
явно и перепроверяются под lock.

```json
{
  "status": "applied",
  "claimId": "...",
  "fencingToken": 7,
  "expectedRunVersion": 5,
  "expectedMessageVersion": 1,
  "safeBoundary": "tool_batch:8:complete",
  "reason": ""
}
```

`status`: `applied|rejected|superseded`. `applied` требует `safeBoundary`.
Acknowledgement идёт по `seq`: нельзя разрешить более позднее сообщение, пока
существует раннее `accepted`.

### Ошибки

- `403 forbidden|run_holder_mismatch`;
- `404 run_not_found|control_message_not_found`;
- `409 run_not_active|stale_claim|run_version_conflict`;
- `409 control_message_version_conflict|control_message_terminal`;
- `409 control_message_out_of_order|run_cancel_requested`;
- `422 invalid_control_operation|invalid_control_message`;
- стандартный error envelope с `code`, `message`, `details`, `requestId`.

Versioning stance: additive v1 API. Legacy
`POST /runs/{id}:request-cancel` остаётся compatibility wrapper и создаёт
типизированное `request_cancel` сообщение с deterministic domain key.

## 6. Authorization и concurrency

Lock order: task → claim (если нужен) → run → control message. Force-cascade
сначала берёт per-tenant transaction advisory lock, затем проходит descendant
graph и берёт task → claim → run locks; это исключает встречный deadlock даже
при циклическом `spawned_by`. Любое
несовпадение Claim/fencing возвращает `409 stale_claim` без partial writes.

`expectedRunVersion` обязателен для create/ack. `expectedMessageVersion`
обязателен для acknowledgement. Каждая accepted/resolved transition повышает
`run.version`; acknowledgement повышает `message.version`.

После `request_cancel → applied` `record_run_action` отклоняет новые actions с
`409 run_cancel_requested`; checkpoint/final cancel остаются разрешены. После
`force_cancel` Run terminal и Claim released, поэтому все owner-only writes
отклоняются существующими run/fencing gates.

## 7. Events и audit

- `run.control_message.accepted`;
- `run.control_message.applied|rejected|superseded`;
- compatibility event `run.cancel_requested` для `request_cancel`;
- `run.cancelled` + `claim.released` для `force_cancel`;
- child cascade создаёт отдельное control message/event на каждый child Run.

Event payload хранит только ids, seq, operation, status, causal position и
safe-boundary marker. Directive/reason не попадают в journal/outbox.

## 8. Failure model

- Ambiguous response: HTTP и domain idempotency возвращают исходный result.
- Restart до acknowledgement: сообщение остаётся `accepted` и читается снова.
- Crash после acknowledgement: status/event commit атомарны.
- Concurrent controllers: `run.version` сериализует решения; проигравший
  перечитывает Run и очередь.
- Lost Claim: acknowledgement получает `stale_claim` и не меняет сообщение.
- Apply result против force cancel: task/run locks дают один serial order;
  поздняя authoritative write отклоняется.
- Parent/child cascade: одна транзакция; partial cancellation невозможна.
- Event delivery: at-least-once; состояние таблицы авторитетно.

## 9. Out of scope

- process kill/supervisor;
- transport к model provider;
- UI Stop/Redirect;
- transcript/session store;
- child result/handle contract HRS-7;
- arbitrary workflow engine;
- автоматическая запись Memory.

## 10. Acceptance и verification

1. Happy-path API/MCP для всех операций.
2. Race: steer во время model/tool markers, cancel против successful action,
   redirect после partial response.
3. Restart between accepted/applied сохраняет ровно одно сообщение.
4. Force cancel fencing блокирует zombie writes.
5. Parent force cancel атомарно останавливает active `spawned_by` child Runs.
6. Tenant isolation, permission revocation, idempotency и pagination.
7. Migration upgrade/downgrade/upgrade и полный test/lint/type gate.
