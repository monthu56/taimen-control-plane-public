#!/usr/bin/env python3
"""v0.4 full-system E2E: real Control Plane Docker + real Memory Service.

Prerequisites (both stacks up, fresh volumes):

    # Memory Service (project cp-memtest, port 8079, offline providers)
    cd ../memory-service/deploy && docker compose -p cp-memtest \
        --env-file <env> up -d --build

    # Control Plane (adapter wired to the memory instance)
    CP_CONTEXT_PROVIDER=http \
    CP_CONTEXT_BASE_URL=http://host.docker.internal:8079 \
    CP_CONTEXT_API_KEY=<key> docker compose up -d --build \
        db api worker context-adapter

Runs, in order, printing PASS/FAIL per step:

  1. New-session continuity (§59/§92): session A claims a task, runs,
     records an explicit finding, publishes an artifact, checkpoints and
     disappears; a brand-new client with no prior state asks for context and
     receives BOTH the authoritative current state and the recalled finding.
  2. Memory outage (§60/§95): memory container stopped; coordination and
     readiness unaffected; work continues; container restarted; the adapter
     catches up; nothing lost, nothing duplicated.
  3. Adapter crash ambiguity (§94): the adapter is killed between provider
     confirm and cursor commit (simulated by cursor reset); re-delivery is
     deduplicated by the Memory Service.

Exit code 0 = every scenario passed.
"""

import subprocess
import sys
import time
import uuid

import httpx

CP = "http://127.0.0.1:8000"
MEMORY = "http://127.0.0.1:8079"
MEMORY_KEY = "memtest-api-key-0123456789"
BOOTSTRAP_TOKEN = "dev-bootstrap-token-change-me"
MEMORY_COMPOSE = ["docker", "compose", "-p", "cp-memtest"]

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def wait_for(predicate, timeout: float = 60.0, interval: float = 1.0, label: str = "") -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    print(f"  [timeout] waiting for {label}")
    return False


def memory_observations(namespace: str) -> list[dict]:
    with httpx.Client(base_url=MEMORY, headers={"Authorization": f"Bearer {MEMORY_KEY}"}) as http:
        return (
            http.get("/api/memory/observations", params={"namespace": namespace, "limit": 500})
            .json()
            .get("observations", [])
        )


def main() -> int:
    api = httpx.Client(base_url=CP, timeout=30)

    print("== Setup: bootstrap tenant, principals, workspace, task ==")
    wait_for(lambda: api.get("/health/ready").status_code == 200, label="CP ready")
    boot = api.post(
        "/api/v1/bootstrap",
        json={"tenantSlug": "acme", "tenantName": "Acme", "adminDisplayName": "Admin"},
        headers={"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"},
    )
    if boot.status_code != 201:
        print(f"bootstrap failed ({boot.status_code}): {boot.text} — need fresh volumes")
        return 2
    admin_key = boot.json()["apiKey"]["key"]
    tenant_id = boot.json()["tenant"]["id"]
    namespace = f"tenant:{tenant_id}"
    admin = {"Authorization": f"Bearer {admin_key}"}

    human = api.post(
        "/api/v1/principals", json={"kind": "human", "displayName": "Dev"}, headers=admin
    ).json()
    human_key = api.post(
        f"/api/v1/principals/{human['id']}/api-keys",
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
            ]
        },
        headers=admin,
    ).json()["key"]
    dev = {"Authorization": f"Bearer {human_key}"}

    workspace = api.post(
        "/api/v1/workspaces", json={"slug": "engineering", "name": "Engineering"}, headers=admin
    ).json()
    task = api.post(
        "/api/v1/tasks",
        json={
            "title": "Fix the replay race in the exporter",
            "description": "Events are lost on concurrent commits",
            "workspaceId": workspace["id"],
        },
        headers=admin,
    ).json()

    print("== Scenario 1: session A works, remembers, vanishes ==")
    session_a = api.post(
        "/api/v1/sessions",
        json={
            "clientName": "e2e-harness-a",
            "harness": {"type": "cli", "protocolVersion": "2", "capabilities": ["resume"]},
        },
        headers=dev,
    ).json()
    claim = api.post(
        f"/api/v1/tasks/{task['id']}:claim",
        json={"sessionId": session_a["id"]},
        headers=dev,
    ).json()
    run = api.post(
        f"/api/v1/tasks/{task['id']}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=dev,
    ).json()
    context_a = api.post("/api/v1/context", json={"task": task["id"]}, headers=dev).json()
    check(
        "session A got working context",
        context_a["operational"]["focus"]["task"]["id"] == task["id"],
    )

    finding = api.post(
        "/api/v1/observations",
        json={
            "kind": "finding",
            "content": "The race is caused by xid/sequence inversion"
            " between insert and commit order",
            "task": task["id"],
            "runId": run["id"],
            "sessionId": session_a["id"],
        },
        headers=dev,
    )
    check("explicit finding recorded", finding.status_code == 201, finding.text)
    artifact = api.post(
        "/api/v1/artifacts",
        json={
            "type": "analysis",
            "name": "race-analysis.md",
            "task": task["id"],
            "runId": run["id"],
            "uri": "repo://docs/race-analysis.md",
        },
        headers=dev,
    )
    check("artifact created", artifact.status_code == 201, artifact.text)
    checkpoint = api.post(
        f"/api/v1/runs/{run['id']}/checkpoints",
        json={"kind": "working_state", "data": {"nextStep": "write regression test"}},
        headers=dev,
    )
    check("checkpoint created", checkpoint.status_code == 201, checkpoint.text)
    suspended = api.post(
        f"/api/v1/runs/{run['id']}:suspend", json={"reason": "end of day"}, headers=dev
    )
    check("run suspended", suspended.status_code == 200, suspended.text)
    api.post(f"/api/v1/sessions/{session_a['id']}:close", headers=dev)
    # Session A's process is gone. No local state survives.

    print("== Adapter delivers to Memory ==")
    delivered = wait_for(
        lambda: any(
            o["kind"] == "finding" and "inversion" in o.get("content", "")
            for o in memory_observations(namespace)
        ),
        timeout=60,
        label="finding in memory",
    )
    check("finding delivered to Memory", delivered)

    print("== Scenario 1 continued: brand-new session B continues Task X ==")
    fresh = httpx.Client(base_url=CP, timeout=30, headers=dev)  # no state from A
    context_b = fresh.post(
        "/api/v1/context",
        json={"task": task["id"], "query": "continue this task"},
        headers=dev,
    ).json()
    operational = context_b["operational"]
    check("authoritative task state present", operational["focus"]["task"]["id"] == task["id"])
    check(
        "artifacts visible operationally",
        any(a["name"] == "race-analysis.md" for a in operational["focus"]["artifacts"]),
    )
    check("suspended run visible", any(r["id"] == run["id"] for r in operational["suspendedRuns"]))
    check("memory pack returned", context_b["memoryStatus"] == "ok", context_b["memoryStatus"])
    pack_texts = [
        item.get("text", "")
        for section in (context_b["memory"] or {}).get("sections", [])
        for item in section.get("items", [])
    ]
    check(
        "previous finding recalled with provenance",
        any("xid/sequence inversion" in t for t in pack_texts),
        str(pack_texts)[:300],
    )
    check("trace id present", bool(context_b["memoryTraceId"]))
    lag = context_b["freshness"]
    check("freshness cursors present", bool(lag["currentCursor"]) and bool(lag["memoryCursor"]))

    print("== Scenario 2: memory outage does not block coordination ==")
    subprocess.run([*MEMORY_COMPOSE, "stop", "memory-service"], check=True, capture_output=True)
    during: list[str] = []
    for i in range(3):
        t = api.post("/api/v1/tasks", json={"title": f"During outage {i}"}, headers=admin)
        check(f"task {i} created during outage", t.status_code == 201)
        during.append(t.json()["id"])
    outage_finding = api.post(
        "/api/v1/observations",
        json={"kind": "note", "content": f"Recorded during outage {uuid.uuid4().hex[:6]}"},
        headers=dev,
    )
    check("observation recorded during outage", outage_finding.status_code == 201)
    ready = api.get("/health/ready")
    check("readiness stays healthy", ready.status_code == 200, ready.text)
    degraded = api.post("/api/v1/context", json={}, headers=dev).json()
    check(
        "context degrades gracefully",
        degraded["memoryStatus"] in ("unavailable", "timeout") and degraded["operational"],
        degraded["memoryStatus"],
    )

    subprocess.run([*MEMORY_COMPOSE, "start", "memory-service"], check=True, capture_output=True)
    wait_for(lambda: httpx.get(f"{MEMORY}/healthz").status_code == 200, label="memory back")
    caught_up = wait_for(
        lambda: (
            any("During outage" in o.get("content", "") for o in memory_observations(namespace))
            and any(
                "Recorded during outage" in o.get("content", "")
                for o in memory_observations(namespace)
            )
        ),
        timeout=90,
        label="catch-up",
    )
    check("missed observations arrive after restart", caught_up)
    recall = fresh.post("/api/v1/context", json={"task": task["id"]}, headers=dev).json()
    check(
        "context works again after outage", recall["memoryStatus"] == "ok", recall["memoryStatus"]
    )

    print("== Scenario 3: adapter crash ambiguity -> deduplicated redelivery ==")
    observations_before = memory_observations(namespace)
    semantic_before = {o["observation_id"] for o in observations_before}
    # Kill the adapter, rewind its cursor (crash between confirm and commit),
    # restart: the same suffix is re-sent.
    subprocess.run(
        ["docker", "compose", "stop", "context-adapter"], check=True, capture_output=True
    )
    subprocess.run(
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
            "-c",
            "UPDATE event_consumer_cursors SET tx_id = 0, sequence = 0"
            " WHERE name = 'context-adapter'",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["docker", "compose", "start", "context-adapter"], check=True, capture_output=True
    )

    def cursor_caught_up() -> bool:
        row = subprocess.run(
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
                "SELECT sequence FROM event_consumer_cursors WHERE name = 'context-adapter'",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return bool(row) and int(row) > 0

    check(
        "adapter re-delivered and advanced",
        wait_for(cursor_caught_up, timeout=60, label="cursor advance"),
    )
    observations_after = memory_observations(namespace)
    semantic_after = {o["observation_id"] for o in observations_after}
    check(
        "re-delivery produced no duplicate semantic observations",
        semantic_after == semantic_before and len(observations_after) == len(observations_before),
        f"before={len(observations_before)} after={len(observations_after)}",
    )

    print()
    if FAILURES:
        print(f"E2E FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("E2E PASSED: continuity, outage/catch-up, crash-ambiguity all green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
