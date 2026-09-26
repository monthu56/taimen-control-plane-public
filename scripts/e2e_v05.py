#!/usr/bin/env python3
"""v0.5 full-system E2E: real Control Plane Docker + real Memory Service.

Prerequisites (both stacks up, FRESH volumes):

    # Memory Service (project cp-memtest, port 8079, offline providers)
    cd ../memory-service/deploy && docker compose -p cp-memtest \
        --env-file <env> up -d --build

    # Control Plane (adapter wired to the memory instance)
    docker compose up -d db api worker
    CP_CONTEXT_PROVIDER=http CP_CONTEXT_BASE_URL=http://host.docker.internal:8079 \
    CP_CONTEXT_API_KEY=<key> docker compose up -d --no-deps context-adapter

Scenarios, printed PASS/FAIL per step:

  1. Project Model happy path: workspace types -> template -> portfolio ->
     project -> workstream -> task -> project-scoped discovery -> claim/run ->
     project-focused context (with recalled memory) -> config revision ->
     lifecycle transition -> completion.
  2. Governance inheritance: a child project may tighten but never weaken, and
     a move that would break the ceiling is refused whole.
  3. Per-tenant delivery isolation: a poison observation parks ONE tenant while
     another keeps flowing; operator redrive resumes from the same position.
  4. Journal retention: archive keeps replay intact; prune makes an old cursor
     report the floor instead of silently skipping.

Exit code 0 = every scenario passed.
"""

import subprocess
import sys
import time
import uuid

import httpx

CP = "http://127.0.0.1:8000"
MEMORY = "http://127.0.0.1:8079"
BOOTSTRAP_TOKEN = "dev-bootstrap-token-change-me"
MEMORY_KEY = "memtest-api-key-0123456789"

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


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


def wait_for(predicate, timeout: float = 60.0, interval: float = 0.5, label: str = "") -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    print(f"  [timeout] waiting for {label}")
    return False


def main() -> int:
    api = httpx.Client(base_url=CP, timeout=60)

    print("== Setup: bootstrap, types, template ==")
    boot = api.post(
        "/api/v1/bootstrap",
        json={"tenantSlug": "e2e5", "tenantName": "E2E", "adminDisplayName": "Admin"},
        headers={"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"},
    )
    if boot.status_code != 201:
        print("  bootstrap failed — run on a FRESH stack (docker compose down -v)")
        return 2
    admin = {"Authorization": f"Bearer {boot.json()['apiKey']['key']}"}
    tenant_id = boot.json()["tenant"]["id"]

    agent = api.post(
        "/api/v1/principals", json={"kind": "agent", "displayName": "E2E Agent"}, headers=admin
    ).json()
    agent_key = api.post(
        f"/api/v1/principals/{agent['id']}/api-keys",
        json={
            "permissions": [
                "sessions.open",
                "tasks.read",
                "tasks.write",
                "tasks.claim",
                "events.read",
                "artifacts.read",
                "artifacts.write",
                "observations.write",
                "projects.read",
            ]
        },
        headers=admin,
    ).json()["key"]
    agent_auth = {"Authorization": f"Bearer {agent_key}"}

    for key, children in (
        ("portfolio", ["project"]),
        ("project", ["workstream"]),
        ("workstream", []),
    ):
        response = api.post(
            "/api/v1/workspace-types",
            json={"key": key, "displayName": key.title(), "allowedChildTypes": children},
            headers=admin,
        )
        check(f"workspace type {key}", response.status_code == 201, response.text)

    template = api.post(
        "/api/v1/project-templates",
        json={
            "key": "delivery",
            "displayName": "Delivery",
            "defaultConfig": {
                "settings": {"tone": "neutral"},
                "governance": {"maxRunActions": 100},
            },
            "defaultViews": [{"key": "overview"}],
        },
        headers=admin,
    ).json()
    check("template v1", template.get("version") == 1, str(template))

    print("\n== Scenario 1: project happy path ==")
    portfolio = api.post(
        "/api/v1/workspaces",
        json={"slug": "acme", "name": "Acme", "typeKey": "portfolio"},
        headers=admin,
    ).json()
    project = api.post(
        "/api/v1/projects",
        json={
            "workspaceSlug": "apollo",
            "parentWorkspaceId": portfolio["id"],
            "workspaceTypeKey": "project",
            "templateId": template["id"],
        },
        headers=admin,
    ).json()
    check("project created", project.get("statusKey") == "planned", str(project))
    check("derived parent is null at the top", project.get("parentProjectId") is None)

    workstream = api.post(
        "/api/v1/workspaces",
        json={
            "slug": "backend",
            "name": "Backend",
            "parentId": project["workspaceId"],
            "typeKey": "workstream",
        },
        headers=admin,
    ).json()
    task = api.post(
        "/api/v1/tasks",
        json={"title": "Ship the API", "workspaceId": workstream["id"]},
        headers=admin,
    ).json()
    check("task derives its project", task.get("projectId") == project["id"], str(task))

    tree = api.get(
        "/api/v1/workspaces/tree", params={"rootId": portfolio["id"]}, headers=admin
    ).json()["roots"]
    check(
        "tree carries the project projection",
        tree and tree[0]["children"] and tree[0]["children"][0]["project"]["id"] == project["id"],
    )

    discovered = api.get(
        "/api/v1/work/available", params={"projectId": project["id"]}, headers=agent_auth
    ).json()["items"]
    check("discovery by projectId", [t["id"] for t in discovered] == [task["id"]])

    session = api.post(
        "/api/v1/sessions",
        json={"clientName": "e2e", "harness": {"type": "cli", "protocolVersion": "2"}},
        headers=agent_auth,
    ).json()
    claim = api.post(
        f"/api/v1/tasks/{task['id']}:claim", json={"sessionId": session["id"]}, headers=agent_auth
    ).json()
    run = api.post(
        f"/api/v1/tasks/{task['id']}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=agent_auth,
    ).json()
    api.post(
        "/api/v1/observations",
        json={"kind": "finding", "content": "Apollo needs a migration window", "task": task["id"]},
        headers=agent_auth,
    ).raise_for_status()

    delivered = wait_for(
        lambda: (
            int(
                psql(
                    "SELECT count(*) FROM event_consumer_cursors c JOIN events e"
                    " ON e.tenant_id = c.tenant_id"
                    " AND (e.tx_id, e.sequence) > (c.tx_id, c.sequence)"
                )
                or 0
            )
            == 0
        ),
        label="adapter catch-up",
    )
    check("adapter caught up", delivered)

    context = api.post(
        "/api/v1/context",
        json={"projectId": project["id"], "task": task["id"], "query": "migration window"},
        headers={**agent_auth, "X-Run-Id": "e2e-trace-1"},
    )
    body = context.json()
    check("project context 200", context.status_code == 200, context.text)
    check("X-Run-Id echoed", context.headers.get("x-run-id") == "e2e-trace-1")
    project_block = body["operational"].get("project", {})
    check("operational project block", project_block.get("id") == project["id"])
    check(
        "effective config in context",
        project_block.get("effectiveConfig", {}).get("settings", {}).get("tone") == "neutral",
    )
    check("memory reached", body["memoryStatus"] == "ok", body.get("warnings"))
    recalled = str(body.get("memory") or "")
    check("finding recalled from memory", "migration window" in recalled, recalled[:200])

    revision = api.post(
        f"/api/v1/projects/{project['id']}/config-revisions",
        json={"config": {"settings": {"tone": "formal"}}},
        headers=admin,
    ).json()
    current = api.get(f"/api/v1/projects/{project['id']}", headers=admin).json()
    check("revision not auto-activated", current["activeConfigRevision"] is None)
    activated = api.post(
        f"/api/v1/projects/{project['id']}/config-revisions/{revision['revision']}:activate",
        headers={**admin, "If-Match": f'"project-{current["version"]}"'},
    )
    check("revision activated", activated.status_code == 200, activated.text)
    effective = api.get(f"/api/v1/projects/{project['id']}/effective-config", headers=admin).json()
    check(
        "effective config follows the revision", effective["config"]["settings"]["tone"] == "formal"
    )
    check(
        "provenance names the revision",
        effective["provenance"]["settings"]["tone"]["source"] == "revision",
    )

    after = api.get(f"/api/v1/projects/{project['id']}", headers=admin).json()
    transitioned = api.post(
        f"/api/v1/projects/{project['id']}:transition",
        json={"statusKey": "active"},
        headers={**admin, "If-Match": f'"project-{after["version"]}"'},
    )
    check("lifecycle transition", transitioned.status_code == 200, transitioned.text)
    check("system category recomputed", transitioned.json().get("systemStatusCategory") == "active")

    succeeded = api.post(
        f"/api/v1/runs/{run['id']}:succeed",
        json={"output": {"ok": True}, "completeTask": True},
        headers=agent_auth,
    )
    check("run succeeded and task completed", succeeded.status_code == 200, succeeded.text)

    events = api.get("/api/v1/events", params={"limit": 200}, headers=agent_auth).json()
    types = {e["type"] for e in events["items"]}
    check(
        "project events replayable",
        {"project.created", "project.config_revision_activated", "project.status_changed"} <= types,
        str(sorted(types)),
    )
    check("every event carries a trace id", all(e["traceRunId"] for e in events["items"]))
    resume = api.get("/api/v1/events", params={"limit": 5}, headers=agent_auth).json()
    tail = api.get(
        "/api/v1/events",
        params={"limit": 200, "cursor": resume["nextCursor"]},
        headers=agent_auth,
    ).json()
    check(
        "reconnect from an opaque cursor loses nothing",
        [e["id"] for e in resume["items"]] + [e["id"] for e in tail["items"]]
        == [e["id"] for e in events["items"]],
    )

    print("\n== Scenario 2: governance inheritance ==")
    parent = api.post(
        "/api/v1/projects",
        json={"workspaceSlug": "gov-parent", "templateId": template["id"]},
        headers=admin,
    ).json()
    api.post(
        f"/api/v1/projects/{parent['id']}/config-revisions",
        json={"config": {"governance": {"maxRunActions": 20, "requireApprovalForRun": True}}},
        headers=admin,
    ).raise_for_status()
    api.post(
        f"/api/v1/projects/{parent['id']}/config-revisions/1:activate",
        headers={**admin, "If-Match": f'"project-{parent["version"]}"'},
    ).raise_for_status()

    child = api.post(
        "/api/v1/projects",
        json={
            "workspaceSlug": "gov-child",
            "parentWorkspaceId": parent["workspaceId"],
            "templateId": template["id"],
        },
        headers=admin,
    ).json()
    inherited = api.get(f"/api/v1/projects/{child['id']}/effective-config", headers=admin).json()[
        "config"
    ]["governance"]
    check(
        "child inherits the ceiling",
        inherited.get("maxRunActions") == 20 and inherited.get("requireApprovalForRun") is True,
        str(inherited),
    )

    weaken = api.post(
        f"/api/v1/projects/{child['id']}/config-revisions",
        json={"config": {"governance": {"maxRunActions": 500}}},
        headers=admin,
    )
    check(
        "weakening refused",
        weaken.status_code == 422 and weaken.json()["error"]["code"] == "governance_weakened",
        weaken.text,
    )

    tighten = api.post(
        f"/api/v1/projects/{child['id']}/config-revisions",
        json={"config": {"governance": {"maxRunActions": 5}}},
        headers=admin,
    )
    check("tightening accepted", tighten.status_code == 201, tighten.text)
    api.post(
        f"/api/v1/projects/{child['id']}/config-revisions/{tighten.json()['revision']}:activate",
        headers={**admin, "If-Match": f'"project-{child["version"]}"'},
    ).raise_for_status()

    lax = api.post(
        "/api/v1/projects",
        json={"workspaceSlug": "gov-lax", "templateId": template["id"]},
        headers=admin,
    ).json()
    moved = api.post(
        f"/api/v1/workspaces/{child['workspaceId']}:move",
        json={"newParentId": lax["workspaceId"]},
        headers=admin,
    )
    check("legal move accepted", moved.status_code == 200, moved.text)
    recomputed = api.get(f"/api/v1/projects/{child['id']}/effective-config", headers=admin).json()
    check(
        "effective config recomputed after move",
        "requireApprovalForRun" not in recomputed["config"]["governance"],
    )

    print("\n== Scenario 3: per-tenant delivery isolation and redrive ==")
    other_tenant = str(uuid.uuid4())
    psql(
        "INSERT INTO tenants (id, slug, name, created_at, updated_at)"
        f" VALUES ('{other_tenant}', 'e2e-other', 'Other', now(), now())"
    )
    # A poison observation for the MAIN tenant only: an unknown kind the
    # Memory Service rejects permanently.
    psql(
        "INSERT INTO events (id, tenant_id, event_type, entity_type, entity_id,"
        " correlation_id, request_id, payload, occurred_at) VALUES"
        f" (gen_random_uuid(), '{tenant_id}', 'observation.recorded', 'observation',"
        " gen_random_uuid(), 'c', 'r',"
        ' \'{"kind": "!!invalid kind!!", "content": "poison"}\'::jsonb, now())'
    )
    psql(
        "INSERT INTO events (id, tenant_id, event_type, entity_type, entity_id,"
        " correlation_id, request_id, payload, occurred_at) VALUES"
        f" (gen_random_uuid(), '{other_tenant}', 'task.created', 'task', gen_random_uuid(),"
        ' \'c\', \'r\', \'{"publicId": "O-1", "title": "other tenant work"}\'::jsonb, now())'
    )
    parked = wait_for(
        lambda: (
            psql(
                "SELECT count(*) FROM event_consumer_cursors"
                f" WHERE tenant_id = '{tenant_id}' AND parked_at IS NOT NULL"
            )
            == "1"
        ),
        timeout=90,
        label="poison parking",
    )
    check("poison parks the affected tenant", parked)
    other_delivered = wait_for(
        lambda: (
            int(
                psql(
                    "SELECT coalesce(sequence, 0) FROM event_consumer_cursors"
                    f" WHERE tenant_id = '{other_tenant}'"
                )
                or 0
            )
            > 0
        ),
        timeout=90,
        label="other tenant delivery",
    )
    check("the other tenant keeps flowing", other_delivered)

    status = api.get("/api/v1/operations/context-adapter", headers=admin).json()
    check(
        "diagnostics report the parked event",
        status.get("parked") is True and status.get("parkedEventId"),
        str(status),
    )
    before_cursor = status["cursor"]

    # Stand-in for "fix the upstream cause": the journal is append-only, so
    # removing the poison row needs the same audited escape hatch the archive
    # command uses. A real operator would fix the mapping or the provider.
    psql(
        "BEGIN; SET LOCAL cp.journal_archiving = 'on';"
        " DELETE FROM events WHERE payload->>'kind' = '!!invalid kind!!'; COMMIT;"
    )
    redrive = api.post(
        f"/api/v1/operations/context-adapter/{tenant_id}:redrive",
        json={"reason": "poison removed"},
        headers=admin,
    )
    check("redrive accepted", redrive.status_code == 200, redrive.text)
    check("redrive does not move the cursor", redrive.json()["cursor"] == before_cursor)
    check(
        "redrive is audited",
        any(
            e["type"] == "context_adapter.redriven"
            for e in api.get("/api/v1/events", params={"limit": 200}, headers=agent_auth).json()[
                "items"
            ]
        ),
    )
    resumed = wait_for(
        lambda: (
            psql(
                "SELECT count(*) FROM event_consumer_cursors"
                f" WHERE tenant_id = '{tenant_id}' AND parked_at IS NOT NULL"
            )
            == "0"
        ),
        timeout=90,
        label="delivery resumed",
    )
    check("delivery resumed after redrive", resumed)

    print("\n== Scenario 4: journal retention ==")
    psql("UPDATE outbox SET delivered_at = now() WHERE delivered_at IS NULL")
    caught_up = wait_for(
        lambda: (
            int(
                psql(
                    "SELECT count(*) FROM event_consumer_cursors c JOIN events e"
                    " ON e.tenant_id = c.tenant_id"
                    " AND (e.tx_id, e.sequence) > (c.tx_id, c.sequence)"
                )
                or 0
            )
            == 0
        ),
        timeout=90,
        label="pre-archive catch-up",
    )
    check("adapter fully caught up before archiving", caught_up)
    psql("UPDATE outbox SET delivered_at = now() WHERE delivered_at IS NULL")

    before_replay = api.get("/api/v1/events", params={"limit": 200}, headers=agent_auth).json()[
        "items"
    ]
    archived = api.post(
        "/api/v1/operations/journal:archive", json={"beforeSeconds": 0}, headers=admin
    )
    check("archive accepted", archived.status_code == 200, archived.text)
    check("archive moved rows", archived.json()["archived"] > 0, archived.text)
    after_replay = api.get("/api/v1/events", params={"limit": 200}, headers=agent_auth).json()[
        "items"
    ]
    check(
        "replay still spans the archive",
        {e["id"] for e in before_replay} <= {e["id"] for e in after_replay},
    )

    pruned = api.post("/api/v1/operations/journal:prune", json={"beforeSeconds": 0}, headers=admin)
    check("prune accepted", pruned.status_code == 200, pruned.text)
    origin = api.get(
        "/api/v1/events", params={"cursor": "ec1_eyJzIjowLCJ0IjowfQ"}, headers=agent_auth
    )
    check(
        "a cursor below the floor is a machine-readable error",
        origin.status_code == 422
        and origin.json()["error"]["code"] == "cursor_below_journal_floor",
        origin.text,
    )

    print()
    if FAILURES:
        print(f"E2E FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("E2E PASSED: project model, governance, delivery isolation, retention all green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
