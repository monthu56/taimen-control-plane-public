# TASK-000003: Durable Active Turn Control — Verification

Date: 2026-08-12

Branch: `codex/task-000003-active-turn-control`

Base: control-plane v0.6.0 (`d267c65`)

Result: PASS; completed and integrated into `main`

## Delivered contract

- PostgreSQL-canonical `run_control_messages` with Run-local ordering,
  lifecycle, optimistic versions and domain idempotency.
- `queue`, `steer`, `redirect`, `request_cancel` and `force_cancel` operations.
- Create, paginated list and holder acknowledgement HTTP endpoints, Python SDK
  methods and MCP tools.
- Cooperative cancel action gate and immediate server-authoritative force
  cancellation with Claim release and atomic `spawned_by` descendant cascade.
- Additive legacy `:request-cancel` compatibility and capability advertisement
  `active_turn_control.v1`.
- Events omit directive/reason content; sensitive directive validation rejects
  credential-like input and absolute local paths.

## Current-session verification evidence

| Gate | Result |
|---|---|
| Focused active-turn, concurrency, SDK/MCP and migration tests | 53 passed |
| Full suite: `uv run pytest` | 483 passed, 13 skipped; 17 Alembic deprecation warnings |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | 259 files already formatted |
| `uv run mypy src` | passed; 109 source files |
| `git diff --check` | passed |
| `uv run alembic heads` | single head `c91f3c7ad8e2` |
| `docker compose build` | passed after transient GHCR TLS timeouts on the first two attempts |
| Fresh Docker readiness | live `alive`; ready `ready`; schema revision `c91f3c7ad8e2` |
| Fresh Docker API E2E | `steer` accepted, acknowledged as `applied` at `turn:1:model-complete`, listed once; Run version 3 |

The isolated Docker stack used alternate host ports. Its containers, network
and test-only database volume were removed after verification.

## Acceptance coverage

- Happy paths and validation for all five operations: covered by integration
  and SDK/MCP tests.
- Concurrent controllers, idempotency, cancel/action and force-cancel races:
  covered by concurrency tests and row/advisory locking.
- Restart/re-read semantics: covered by durable list tests and fresh Docker E2E.
- Stale fencing and zombie writes: covered by integration/concurrency tests.
- Parent/child force-cancel atomicity: covered, including cyclic graph safety.
- Tenant isolation, permissions, opaque cursor binding and pagination limits:
  covered by integration tests.
- Migration upgrade/downgrade/upgrade: covered by migration tests; deployed
  Docker instance reported the new head.

## Main integration

TASK-000004 added Effective Harness Manifest from the same Alembic parent.
Integration preserves both contracts and joins revisions `9c41ee0d7b52` and
`c91f3c7ad8e2` through merge head `d4e6f8a1b2c3`.

Post-merge verification on `main`:

- focused combined runtime/migration/SDK/MCP gate: 101 passed;
- full suite: 553 passed, 13 skipped;
- Ruff, format and mypy: passed;
- Alembic: exactly one head, `d4e6f8a1b2c3`.

## Completion boundary

The human operator explicitly confirmed completion. The Control Plane Run
finished as `succeeded`, and TASK-000003 transitioned to `done`.
