#!/usr/bin/env python3
"""v0.5 performance checks against the local Docker stack (port 8000).

Everything v0.5 added is derived rather than stored (ADR-0035), so the point
of this script is to find out what that derivation actually costs:

  1. ``GET /workspaces/tree`` over a >= 10 000-node tree (one recursive CTE,
     no N+1) at several depths.
  2. Owning-project resolution for a page of tasks (the batch CTE behind
     ``TaskOut.projectId``).
  3. ``GET /work/available`` and ``GET /tasks`` with ``projectId`` on a
     >= 10 000-task backlog, exact scope and includeSubprojects, against the
     v0.4 unfiltered baseline measured in the same run.
  4. ``GET /projects/{id}/effective-config`` down a deep but legal project
     chain.
  5. Context Adapter cycle with several active tenants (fair scheduling).

Run against a FRESH stack (`docker compose up -d --build db api worker`);
the script bootstraps its own tenant. EXPLAIN ANALYZE evidence is printed for
the two queries where an index decision would be made.
"""

import statistics
import subprocess
import time
import uuid

import httpx

CP = "http://127.0.0.1:8000"
BOOTSTRAP_TOKEN = "dev-bootstrap-token-change-me"

TREE_SIZE = 10_000
TASK_BACKLOG = 10_000
PROJECT_DEPTH = 16


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


def timed(fn, samples: int = 10) -> tuple[float, float, float]:
    """(min, p50, p95) in milliseconds.

    ``min`` matters for the large-payload cases: the wall clock includes
    transferring a multi-megabyte body across the Docker port mapping, which
    is noisy enough that the median alone would misrepresent the server.
    """
    values = []
    for _ in range(samples):
        started = time.monotonic()
        fn()
        values.append((time.monotonic() - started) * 1000)
    values.sort()
    return values[0], statistics.median(values), values[max(0, int(len(values) * 0.95) - 1)]


def ensure_admin(api: httpx.Client) -> tuple[str, str]:
    boot = api.post(
        "/api/v1/bootstrap",
        json={"tenantSlug": "bench5", "tenantName": "Bench", "adminDisplayName": "B"},
        headers={"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"},
    )
    if boot.status_code == 201:
        return boot.json()["apiKey"]["key"], boot.json()["tenant"]["id"]
    raise SystemExit("run on a fresh stack (docker compose down -v && up -d --build)")


def seed_tree(tenant_id: str, root_id: str, total: int, fanout: int = 10) -> None:
    """A wide, shallow tree: `total` workspaces under `root_id`."""
    psql(
        "INSERT INTO workspaces (id, tenant_id, parent_id, type_id, slug, name, description,"
        " custom_fields, status, version, created_at, updated_at)"
        f" SELECT gen_random_uuid(), '{tenant_id}', '{root_id}',"
        f" (SELECT id FROM workspace_types WHERE tenant_id = '{tenant_id}' AND is_system),"
        " 'l1-' || gs, 'L1 ' || gs, '', '{}'::jsonb, 'active', 1, now(), now()"
        f" FROM generate_series(1, {fanout}) gs"
    )
    per_branch = (total - fanout) // fanout
    psql(
        "INSERT INTO workspaces (id, tenant_id, parent_id, type_id, slug, name, description,"
        " custom_fields, status, version, created_at, updated_at)"
        f" SELECT gen_random_uuid(), '{tenant_id}', p.id,"
        f" (SELECT id FROM workspace_types WHERE tenant_id = '{tenant_id}' AND is_system),"
        " 'l2-' || p.slug || '-' || gs, 'L2', '', '{}'::jsonb, 'active', 1, now(), now()"
        f" FROM workspaces p, generate_series(1, {per_branch}) gs"
        f" WHERE p.tenant_id = '{tenant_id}' AND p.slug LIKE 'l1-%'"
    )


def bench_tree(api: httpx.Client, key: str, tenant_id: str, root_id: str) -> None:
    node_count = psql(f"SELECT count(*) FROM workspaces WHERE tenant_id = '{tenant_id}'")
    print(f"\n== 1. GET /workspaces/tree ({node_count} nodes) ==")
    headers = {"Authorization": f"Bearer {key}"}
    for label, params in (
        ("full tree, with project projection", {"rootId": root_id}),
        ("full tree, no projection", {"rootId": root_id, "includeProjects": "false"}),
        ("depth=1", {"rootId": root_id, "depth": 1}),
        ("depth=2", {"rootId": root_id, "depth": 2}),
    ):

        def call(params=params):
            response = api.get("/api/v1/workspaces/tree", params=params, headers=headers)
            response.raise_for_status()
            return response

        best, p50, p95 = timed(call, samples=9)
        payload = call()
        # The wall-clock above includes transferring and PARSING the document
        # on the client; for a full 10k-node tree that dominates the server
        # time, so report the size that explains it.
        print(
            f"  {label}: min={best:.0f}ms p50={p50:.0f}ms p95={p95:.0f}ms "
            f"({len(payload.content) / 1024:.0f} KiB response)"
        )

    plan = psql(
        "EXPLAIN (ANALYZE, BUFFERS) WITH RECURSIVE tree(id, parent_id, depth) AS ("
        f" SELECT id, parent_id, 0 FROM workspaces WHERE id = '{root_id}'"
        " UNION ALL SELECT c.id, c.parent_id, t.depth + 1 FROM workspaces c"
        " JOIN tree t ON c.parent_id = t.id WHERE t.depth < 64)"
        " SELECT count(*) FROM tree"
    )
    timing = [line for line in plan.splitlines() if "Execution Time" in line]
    print("  tree CTE:", timing[0].strip() if timing else "n/a")


def bench_project_resolution(api: httpx.Client, key: str, tenant_id: str) -> None:
    print("\n== 2. Owning-project resolution for a task page ==")
    headers = {"Authorization": f"Bearer {key}"}
    for limit in (50, 200):

        def call(limit=limit):
            response = api.get("/api/v1/tasks", params={"limit": limit}, headers=headers)
            response.raise_for_status()
            return response

        best, p50, p95 = timed(call)
        body = call().json()
        resolved = sum(1 for t in body["items"] if t.get("projectId"))
        print(
            f"  limit={limit}: min={best:.0f}ms p50={p50:.0f}ms p95={p95:.0f}ms "
            f"({resolved} with a project)"
        )

    plan = psql(
        "EXPLAIN (ANALYZE, BUFFERS) WITH RECURSIVE up(origin, node, parent, depth) AS ("
        " SELECT w.id, w.id, w.parent_id, 0 FROM workspaces w"
        f" WHERE w.tenant_id = '{tenant_id}' LIMIT 200)"
        " SELECT count(*) FROM up"
    )
    timing = [line for line in plan.splitlines() if "Execution Time" in line]
    print("  seed scan:", timing[0].strip() if timing else "n/a")


def bench_discovery(api: httpx.Client, key: str, tenant_id: str, project: dict) -> None:
    print(f"\n== 3. Discovery on a {TASK_BACKLOG}-task backlog ==")
    headers = {"Authorization": f"Bearer {key}"}
    cases = (
        ("v0.4 baseline: no filter", "/api/v1/work/available", {"limit": 50}),
        (
            "v0.4 baseline: workspace subtree",
            "/api/v1/work/available",
            {"limit": 50, "workspaceId": project["workspaceId"], "includeDescendants": "true"},
        ),
        (
            "v0.5: projectId (exact scope)",
            "/api/v1/work/available",
            {"limit": 50, "projectId": project["id"]},
        ),
        (
            "v0.5: projectId + includeSubprojects",
            "/api/v1/work/available",
            {"limit": 50, "projectId": project["id"], "includeSubprojects": "true"},
        ),
        ("v0.5: GET /tasks?projectId", "/api/v1/tasks", {"limit": 50, "projectId": project["id"]}),
    )
    for label, path, params in cases:

        def call(path=path, params=params):
            response = api.get(path, params=params, headers=headers)
            response.raise_for_status()
            return response

        best, p50, p95 = timed(call)
        print(f"  {label}: min={best:.0f}ms p50={p50:.0f}ms p95={p95:.0f}ms")

    plan = psql(
        "EXPLAIN (ANALYZE, BUFFERS) WITH RECURSIVE scope(id, depth) AS ("
        f" SELECT id, 0 FROM workspaces WHERE id = '{project['workspaceId']}'"
        " UNION ALL SELECT c.id, s.depth + 1 FROM workspaces c JOIN scope s ON c.parent_id = s.id"
        " WHERE s.depth < 64 AND NOT EXISTS ("
        " SELECT 1 FROM project_profiles p WHERE p.workspace_id = c.id))"
        f" SELECT count(*) FROM tasks WHERE tenant_id = '{tenant_id}'"
        " AND workspace_id IN (SELECT id FROM scope)"
    )
    timing = [line for line in plan.splitlines() if "Execution Time" in line]
    print("  scope filter:", timing[0].strip() if timing else "n/a")


def bench_effective_config(api: httpx.Client, key: str, template_id: str) -> None:
    print(f"\n== 4. Effective config down a {PROJECT_DEPTH}-deep project chain ==")
    headers = {"Authorization": f"Bearer {key}"}
    parent_workspace: str | None = None
    leaf: dict | None = None
    for depth in range(PROJECT_DEPTH):
        body = {
            "workspaceSlug": f"chain-{depth}-{uuid.uuid4().hex[:6]}",
            "templateId": template_id,
        }
        if parent_workspace is not None:
            body["parentWorkspaceId"] = parent_workspace
        response = api.post("/api/v1/projects", json=body, headers=headers)
        response.raise_for_status()
        leaf = response.json()
        parent_workspace = leaf["workspaceId"]
        revision = api.post(
            f"/api/v1/projects/{leaf['id']}/config-revisions",
            json={"config": {"settings": {f"level{depth}": depth}}},
            headers=headers,
        )
        revision.raise_for_status()
        api.post(
            f"/api/v1/projects/{leaf['id']}/config-revisions/1:activate",
            headers={**headers, "If-Match": f'"project-{leaf["version"]}"'},
        ).raise_for_status()

    assert leaf is not None

    def call():
        response = api.get(f"/api/v1/projects/{leaf['id']}/effective-config", headers=headers)
        response.raise_for_status()
        return response

    best, p50, p95 = timed(call)
    body = call().json()
    print(
        f"  depth={PROJECT_DEPTH}: min={best:.0f}ms p50={p50:.0f}ms p95={p95:.0f}ms "
        f"({len(body['provenance']['layers'])} layers, "
        f"{len(body['config']['settings'])} settings keys)"
    )


def bench_adapter_multi_tenant(tenant_id: str) -> None:
    print("\n== 5. Context adapter with several active tenants ==")
    extra = []
    for index in range(3):
        new_id = str(uuid.uuid4())
        psql(
            "INSERT INTO tenants (id, slug, name, created_at, updated_at)"
            f" VALUES ('{new_id}', 'bench-t{index}', 'Bench {index}', now(), now())"
        )
        psql(
            "INSERT INTO events (id, tenant_id, event_type, entity_type, entity_id,"
            " correlation_id, request_id, payload, occurred_at)"
            f" SELECT gen_random_uuid(), '{new_id}', 'task.created', 'task', gen_random_uuid(),"
            ' \'c\', \'r\', \'{"publicId": "B-1", "title": "bench"}\'::jsonb, now()'
            " FROM generate_series(1, 2000)"
        )
        extra.append(new_id)

    psql(
        "INSERT INTO events (id, tenant_id, event_type, entity_type, entity_id,"
        " correlation_id, request_id, payload, occurred_at)"
        f" SELECT gen_random_uuid(), '{tenant_id}', 'task.created', 'task', gen_random_uuid(),"
        ' \'c\', \'r\', \'{"publicId": "B-1", "title": "bench"}\'::jsonb, now()'
        " FROM generate_series(1, 2000)"
    )
    total = int(psql("SELECT count(*) FROM events"))
    print(f"  journal: {total} events across {len(extra) + 1} tenants")

    psql("DELETE FROM event_consumer_cursors")
    subprocess.run(
        ["docker", "compose", "restart", "context-adapter"], check=True, capture_output=True
    )
    started = time.monotonic()
    deadline = started + 300
    delivered = 0
    while time.monotonic() < deadline:
        rows = psql(
            "SELECT count(*) FROM event_consumer_cursors c JOIN events e"
            " ON e.tenant_id = c.tenant_id AND (e.tx_id, e.sequence) > (c.tx_id, c.sequence)"
        )
        remaining = int(rows or 0)
        delivered = total - remaining
        if remaining == 0:
            break
        time.sleep(0.05)
    elapsed = time.monotonic() - started
    # The clock starts at the container restart, so this INCLUDES process
    # start-up: it is an upper bound on catch-up time, not a pure rate.
    print(
        f"  caught up {delivered} events in {elapsed:.1f}s incl. adapter restart "
        f"(>= {delivered / max(elapsed, 0.001):,.0f} events/s across tenants)"
    )
    parked = psql("SELECT count(*) FROM event_consumer_cursors WHERE parked_at IS NOT NULL")
    print(f"  parked tenants: {parked}")


def main() -> None:
    api = httpx.Client(base_url=CP, timeout=120)
    key, tenant_id = ensure_admin(api)
    headers = {"Authorization": f"Bearer {key}"}

    root = api.post(
        "/api/v1/workspaces", json={"slug": "bench-root", "name": "Root"}, headers=headers
    ).json()
    template = api.post(
        "/api/v1/project-templates",
        json={"key": "bench", "displayName": "Bench"},
        headers=headers,
    ).json()
    project = api.post(
        "/api/v1/projects",
        json={
            "workspaceSlug": "bench-project",
            "parentWorkspaceId": root["id"],
            "templateId": template["id"],
        },
        headers=headers,
    ).json()
    # A nested project so exact scope and includeSubprojects actually differ.
    api.post(
        "/api/v1/projects",
        json={
            "workspaceSlug": "bench-subproject",
            "parentWorkspaceId": project["workspaceId"],
            "templateId": template["id"],
        },
        headers=headers,
    ).raise_for_status()

    print(f"Seeding {TREE_SIZE} workspaces and {TASK_BACKLOG} tasks…")
    seed_tree(tenant_id, root["id"], TREE_SIZE)
    psql(
        "INSERT INTO tasks (id, tenant_id, public_id, workspace_id, title, description,"
        " status, priority, version, claim_epoch, created_by, created_at, updated_at)"
        f" SELECT gen_random_uuid(), '{tenant_id}', 'B5-' || gs, '{project['workspaceId']}',"
        " 'bench task ' || gs, '', 'todo', (ARRAY['low','medium','high'])[1 + gs % 3], 1, 0,"
        f" (SELECT id FROM principals WHERE tenant_id = '{tenant_id}' LIMIT 1), now(), now()"
        f" FROM generate_series(1, {TASK_BACKLOG}) gs"
    )

    bench_tree(api, key, tenant_id, root["id"])
    bench_project_resolution(api, key, tenant_id)
    bench_discovery(api, key, tenant_id, project)
    bench_effective_config(api, key, template["id"])
    bench_adapter_multi_tenant(tenant_id)


if __name__ == "__main__":
    main()
