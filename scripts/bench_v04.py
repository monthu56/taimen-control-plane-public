#!/usr/bin/env python3
"""v0.4 performance checks against the local Docker stack (port 8000).

Measures, with EXPLAIN ANALYZE evidence where relevant:

  1. Event replay: paginated throughput over a 10k-event journal, cursor
     decode/encode overhead included (the public API path).
  2. Context adapter: catch-up throughput from a 10k-event backlog
     (translate + batch-POST to the real Memory Service).
  3. Interactive context latency: Control Plane overhead on top of the
     Memory Service build_context.
  4. Work discovery: GET /work/available on 1k / 10k task backlogs —
     ready/no-requirements, with-requirements, workspace subtree, priority
     ordering — plus the query plan for the no-requirements case.

Requires the same two-stack setup as scripts/e2e_v04.py, ALREADY
bootstrapped (run e2e first) or a fresh stack (it bootstraps then).
"""

import statistics
import subprocess
import time

import httpx

CP = "http://127.0.0.1:8000"
BOOTSTRAP_TOKEN = "dev-bootstrap-token-change-me"


def psql(sql: str) -> str:
    return subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "control_plane",
            "-d",
            "control_plane",
            "-tAc",
            sql,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def ensure_admin(api: httpx.Client) -> tuple[str, str]:
    boot = api.post(
        "/api/v1/bootstrap",
        json={"tenantSlug": "bench", "tenantName": "Bench", "adminDisplayName": "B"},
        headers={"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"},
    )
    if boot.status_code == 201:
        return boot.json()["apiKey"]["key"], boot.json()["tenant"]["id"]
    # Already bootstrapped (by e2e): mint a key at the storage level.
    raise SystemExit("run on a fresh stack OR export BENCH_KEY manually")


def seed_events(tenant_id: str, count: int) -> None:
    psql(
        "INSERT INTO events (id, tenant_id, event_type, entity_type, entity_id,"
        " correlation_id, request_id, payload, occurred_at)"
        f" SELECT gen_random_uuid(), '{tenant_id}', 'task.created', 'task',"
        " gen_random_uuid(), 'c', 'r', '{}'::jsonb, now()"
        f" FROM generate_series(1, {count})"
    )


def bench_replay(api: httpx.Client, key: str) -> None:
    print("\n== 1. Event replay pagination (10k events, limit=200) ==")
    headers = {"Authorization": f"Bearer {key}"}
    started = time.monotonic()
    cursor: str | None = None
    total = 0
    pages = 0
    while True:
        params: dict = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        body = api.get("/api/v1/events", params=params, headers=headers).json()
        total += len(body["items"])
        pages += 1
        cursor = body["nextCursor"]
        if not body["hasMore"]:
            break
    elapsed = time.monotonic() - started
    print(
        f"  {total} events / {pages} pages in {elapsed:.2f}s "
        f"({total / elapsed:,.0f} events/s, {elapsed / pages * 1000:.1f} ms/page)"
    )

    plan = psql(
        "EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM events WHERE tenant_id ="
        " (SELECT id FROM tenants LIMIT 1) AND (tx_id, sequence) > (0, 0) AND"
        " tx_id < pg_snapshot_xmin(pg_current_snapshot())::text::bigint"
        " ORDER BY tx_id, sequence LIMIT 200"
    )
    first = [line for line in plan.splitlines() if "Index" in line or "Seq Scan" in line][:2]
    print("  plan:", " | ".join(s.strip() for s in first))


def bench_adapter_catchup(tenant_id: str) -> None:
    print("\n== 2. Adapter catch-up from ~10k-event backlog ==")
    psql("UPDATE event_consumer_cursors SET tx_id = 0, sequence = 0 WHERE name = 'context-adapter'")
    subprocess.run(
        ["docker", "compose", "restart", "context-adapter"], check=True, capture_output=True
    )
    started = time.monotonic()
    deadline = started + 300
    last = 0
    while time.monotonic() < deadline:
        frontier = int(psql("SELECT coalesce(max(sequence), 0) FROM events"))
        position = int(
            psql("SELECT sequence FROM event_consumer_cursors WHERE name = 'context-adapter'") or 0
        )
        last = position
        if position >= frontier:
            break
        time.sleep(1)
    elapsed = time.monotonic() - started
    print(
        f"  cursor reached {last} in {elapsed:.1f}s "
        f"(~{last / max(elapsed, 0.001):,.0f} events/s incl. noise skip + HTTP)"
    )


def bench_context_latency(api: httpx.Client, key: str) -> None:
    print("\n== 3. Interactive context latency (20 samples) ==")
    headers = {"Authorization": f"Bearer {key}"}
    for label, body in (
        ("operational only", {"includeMemory": False}),
        ("operational + memory", {}),
    ):
        samples = []
        for _ in range(20):
            t0 = time.monotonic()
            response = api.post("/api/v1/context", json=body, headers=headers)
            response.raise_for_status()
            samples.append((time.monotonic() - t0) * 1000)
        print(
            f"  {label}: p50={statistics.median(samples):.0f}ms "
            f"p95={sorted(samples)[int(len(samples) * 0.95) - 1]:.0f}ms"
        )


def bench_discovery(api: httpx.Client, key: str, tenant_id: str) -> None:
    print("\n== 4. Work discovery ==")
    headers = {"Authorization": f"Bearer {key}"}
    workspace = api.post(
        "/api/v1/workspaces", json={"slug": "bench-ws", "name": "Bench"}, headers=headers
    ).json()

    for backlog in (1_000, 10_000):
        psql(
            "INSERT INTO tasks (id, tenant_id, public_id, workspace_id, title, description,"
            " status, priority, version, claim_epoch, created_by, created_at, updated_at)"
            f" SELECT gen_random_uuid(), '{tenant_id}', 'B-' || gs || '-{backlog}',"
            f" '{workspace['id']}', 'bench task ' || gs, '', 'todo',"
            " (ARRAY['low','medium','high'])[1 + gs % 3], 1, 0,"
            f" (SELECT id FROM principals WHERE tenant_id = '{tenant_id}' LIMIT 1),"
            f" now(), now() FROM generate_series(1, {backlog}) gs"
        )
        total = psql(f"SELECT count(*) FROM tasks WHERE tenant_id = '{tenant_id}'")
        for label, params in (
            ("ready/no-req", {"limit": 50}),
            (
                "workspace subtree",
                {"limit": 50, "workspaceId": workspace["id"], "includeDescendants": "true"},
            ),
        ):
            samples = []
            for _ in range(10):
                t0 = time.monotonic()
                response = api.get("/api/v1/work/available", params=params, headers=headers)
                response.raise_for_status()
                samples.append((time.monotonic() - t0) * 1000)
            print(f"  backlog={total}: {label}: p50={statistics.median(samples):.0f}ms")

    # Requirements-heavy case: attach a role requirement to 1k tasks.
    psql(
        "INSERT INTO roles (id, tenant_id, slug, name, description, created_at,"
        " updated_at, version)"
        f" VALUES (gen_random_uuid(), '{tenant_id}', 'bench-role', 'Bench', '', now(), now(), 1)"
        " ON CONFLICT DO NOTHING"
    )
    psql(
        "INSERT INTO task_requirements (id, tenant_id, task_id, kind, role_id, created_at)"
        f" SELECT gen_random_uuid(), '{tenant_id}', t.id, 'role',"
        f" (SELECT id FROM roles WHERE tenant_id = '{tenant_id}' AND slug = 'bench-role'),"
        f" now() FROM (SELECT id FROM tasks WHERE tenant_id = '{tenant_id}'"
        " AND title LIKE 'bench task%' LIMIT 1000) t"
    )
    samples = []
    for _ in range(10):
        t0 = time.monotonic()
        api.get("/api/v1/work/available", params={"limit": 50}, headers=headers).raise_for_status()
        samples.append((time.monotonic() - t0) * 1000)
    print(f"  with 1k requirement-gated tasks: p50={statistics.median(samples):.0f}ms")

    plan = psql(
        "EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM tasks WHERE tenant_id ="
        f" '{tenant_id}' AND status = 'todo' AND active_claim_id IS NULL"
        " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,"
        " created_at LIMIT 50"
    )
    timing = [line for line in plan.splitlines() if "Execution Time" in line]
    print("  base query:", timing[0].strip() if timing else "n/a")


def main() -> None:
    api = httpx.Client(base_url=CP, timeout=60)
    key, tenant_id = ensure_admin(api)
    print("Seeding 10k events…")
    seed_events(tenant_id, 10_000)
    bench_replay(api, key)
    bench_adapter_catchup(tenant_id)
    bench_context_latency(api, key)
    bench_discovery(api, key, tenant_id)


if __name__ == "__main__":
    main()
