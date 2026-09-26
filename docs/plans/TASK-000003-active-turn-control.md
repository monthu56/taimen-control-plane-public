# TASK-000003: Durable Active Turn Control — PLAN

Статус: completed; merged into `main`

Ветка: `codex/task-000003-active-turn-control`

База: control-plane v0.6.0 (`d267c65`)

## Ограничение параллельной работы

TASK-000004 выполняется параллельно от той же Alembic head
`72ef8bc31a06`. Эта ветка получает уникальную revision id. Перед объединением
нужно rebase/cherry-pick на итог TASK-000004 либо отдельная Alembic merge
revision; multiple heads не считаются готовым результатом.

## Вертикальные TDD slices

### S1. Durable accepted message

RED:

- integration test создаёт `steer` через HTTP и читает его после новой DB
  session;
- повтор с тем же Idempotency-Key не создаёт вторую строку/event;
- stale `expectedRunVersion` даёт 409.

GREEN:

- enum/model/migration `run_control_messages`;
- command create, event, schema и POST route;
- directive/operation validation и permissions.

REFACTOR: вынести lock/version/idempotency helpers, сохраняя GREEN.

### S2. Ordered acknowledgement и recovery

RED:

- holder подтверждает message с explicit Claim/fencing;
- stale claim, wrong principal, wrong versions и out-of-order ack отклоняются;
- restart/list from cursor возвращает unresolved message ровно один раз.

GREEN:

- acknowledgement command;
- opaque per-Run cursor и bounded GET page;
- lifecycle events, run context pending projection.

REFACTOR: общий serializer/cursor module без изменения публичного контракта.

### S3. Cooperative cancel boundary

RED:

- `request_cancel` создаёт typed message и legacy projection/event;
- до acknowledgement action разрешён; после `applied` новый action получает
  `409 run_cancel_requested`;
- checkpoint и terminal `:cancel` остаются доступны.

GREEN:

- compatibility wrapper старого endpoint;
- action gate по applied request-cancel;
- client/MCP create/list/ack methods.

REFACTOR: убрать дублирование legacy/new cancel path.

### S4. Force cancel и child cascade

RED:

- без `claims.manage` force cancel запрещён;
- force cancel атомарно terminalizes Run, releases Claim и supersedes pending
  messages;
- active child Runs по `spawned_by` получают derived applied message,
  cancelled state и released claims;
- concurrent success/action после cancel отклоняется.

GREEN:

- deterministic multi-task lock/cascade command;
- domain events/outbox на каждый transition;
- metrics и audit refs без sensitive payload.

REFACTOR: общий terminal cancel helper с существующим `cancel_run`, не ломая
legacy semantics.

### S5. Contract completeness

RED:

- SDK/MCP contract tests;
- OpenAPI request/response/error shape assertions;
- tenant isolation and permission-revocation tests;
- pagination max/cursor mismatch tests.

GREEN:

- SDK and MCP tools;
- `docs/api.md`, `docs/harness-protocol.md`, `docs/architecture.md`;
- migration and compatibility notes.

## Verification matrix

| Gate | Command/evidence |
|---|---|
| Focused integration | `uv run pytest tests/integration/test_active_turn_control.py -q` |
| Concurrency | `uv run pytest tests/concurrency/test_active_turn_control.py -q` |
| Client/MCP | `uv run pytest tests/client/test_sdk.py tests/client/test_mcp_tools.py -q` |
| Migration | isolated PostgreSQL upgrade → downgrade → upgrade |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format --check .` |
| Types | `uv run mypy src` |
| Full tests | `uv run pytest` |
| Docker | `docker compose build` and service health/E2E when daemon available |

## Rollout/rollback

- Schema is additive; old harnesses keep using `:request-cancel`.
- New capabilities are advertised only after server deployment and client
  support.
- Rollback requires no unresolved control messages; downgrade refuses/operates
  only under operator-controlled deployment rollback.
- Events are append-only history and are not deleted by application rollback.

## Artifacts

- SPEC: `docs/specs/TASK-000003-active-turn-control.md`.
- PLAN: this file.
- ADR: create only after spike verification confirms semantics.
- Verification report: `docs/verification/TASK-000003-verification.md`.
- SPEC, PLAN, ADR, commit and verification report registered in Control Plane;
  Run completed after explicit human confirmation.
