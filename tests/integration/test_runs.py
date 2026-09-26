"""Run lifecycle: start under claim, succeed/fail/cancel, zombie fencing."""

import httpx
from sqlalchemy.engine import Engine

from tests.helpers import (
    auth,
    backdate_expiry,
    claim_task,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
    open_session,
)


async def _setup(
    client: httpx.AsyncClient, admin_key: str, name: str = "agent"
) -> tuple[dict, str, dict]:
    principal, key = await create_agent_with_key(client, admin_key, name=name)
    session = await open_session(client, key, client_name=name)
    return principal, key, session


async def _start_run(
    client: httpx.AsyncClient, key: str, task_ref: str, claim: dict, **extra: object
) -> httpx.Response:
    return await client.post(
        f"/api/v1/tasks/{task_ref}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"], **extra},
        headers=auth(key),
    )


async def test_run_happy_path_completes_task(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key, session = await _setup(client, admin_key)
    task = await create_task(client, admin_key)
    claim = (await claim_task(client, agent_key, task["id"], session["id"])).json()

    response = await _start_run(client, agent_key, task["id"], claim, input={"issue": 483})
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "running"
    assert run["attempt"] == 1
    assert run["fencingToken"] == claim["fencingToken"]
    assert run["claimId"] == claim["id"]

    response = await client.post(
        f"/api/v1/runs/{run['id']}:succeed",
        json={"output": {"pr": 42}},
        headers=auth(agent_key),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["run"]["status"] == "succeeded"
    assert payload["run"]["output"] == {"pr": 42}
    assert payload["task"]["status"] == "done"
    assert payload["task"]["activeClaimId"] is None

    claim_after = (
        await client.get(f"/api/v1/claims/{claim['id']}", headers=auth(agent_key))
    ).json()
    assert claim_after["status"] == "released"
    assert claim_after["releaseReason"] == "completed"

    events = (await client.get("/api/v1/events", headers=auth(agent_key))).json()["items"]
    types = [e["type"] for e in events]
    assert "run.started" in types
    assert "run.succeeded" in types
    assert "task.completed" in types
    assert types.index("run.succeeded") < types.index("task.completed")


async def test_run_requires_live_claim_credentials(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key, session = await _setup(client, admin_key)
    task = await create_task(client, admin_key)
    claim = (await claim_task(client, agent_key, task["id"], session["id"])).json()

    # Wrong fencing token
    response = await client.post(
        f"/api/v1/tasks/{task['id']}:start-run",
        json={"claimId": claim["id"], "fencingToken": 999},
        headers=auth(agent_key),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_claim"

    # No claim at all on an unclaimed task
    other = await create_task(client, admin_key, title="Unclaimed")
    response = await client.post(
        f"/api/v1/tasks/{other['id']}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(agent_key),
    )
    assert response.status_code == 409


async def test_only_one_running_run_per_task(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key, session = await _setup(client, admin_key)
    task = await create_task(client, admin_key)
    claim = (await claim_task(client, agent_key, task["id"], session["id"])).json()

    assert (await _start_run(client, agent_key, task["id"], claim)).status_code == 201
    response = await _start_run(client, agent_key, task["id"], claim)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "run_already_active"


async def test_failed_run_allows_retry(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key, session = await _setup(client, admin_key)
    task = await create_task(client, admin_key)
    claim = (await claim_task(client, agent_key, task["id"], session["id"])).json()

    run1 = (await _start_run(client, agent_key, task["id"], claim)).json()
    response = await client.post(
        f"/api/v1/runs/{run1['id']}:fail",
        json={"failureReason": "tests are red"},
        headers=auth(agent_key),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["failureReason"] == "tests are red"

    # Task stays in progress under the same claim; a second attempt starts.
    task_now = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(agent_key))).json()
    assert task_now["status"] == "in_progress"
    run2 = (await _start_run(client, agent_key, task["id"], claim)).json()
    assert run2["attempt"] == 2

    # Finishing an already-finished run conflicts
    response = await client.post(f"/api/v1/runs/{run1['id']}:fail", headers=auth(agent_key))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "run_not_active"


async def test_succeed_without_completing_task(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key, session = await _setup(client, admin_key)
    task = await create_task(client, admin_key)
    claim = (await claim_task(client, agent_key, task["id"], session["id"])).json()
    run = (await _start_run(client, agent_key, task["id"], claim)).json()

    response = await client.post(
        f"/api/v1/runs/{run['id']}:succeed",
        json={"completeTask": False},
        headers=auth(agent_key),
    )
    assert response.status_code == 200
    assert response.json()["task"]["status"] == "in_progress"

    # The classic completion path still works afterwards.
    version = response.json()["task"]["version"]
    response = await client.post(
        f"/api/v1/tasks/{task['id']}:complete",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers={**auth(agent_key), "If-Match": f'"task-{version}"'},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "done"


async def test_classic_complete_blocked_by_own_running_run(
    client: httpx.AsyncClient,
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key, session = await _setup(client, admin_key)
    task = await create_task(client, admin_key)
    claim = (await claim_task(client, agent_key, task["id"], session["id"])).json()
    await _start_run(client, agent_key, task["id"], claim)

    response = await client.post(
        f"/api/v1/tasks/{task['id']}:complete",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers={**auth(agent_key), "If-Match": '"task-2"'},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "run_in_progress"


async def test_zombie_run_cannot_finish_task_after_takeover(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The canonical fencing scenario extended through the Run lifecycle."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, key_a, session_a = await _setup(client, admin_key, "agent-a")
    _, key_b, session_b = await _setup(client, admin_key, "agent-b")
    task = await create_task(client, admin_key)

    claim_a = (await claim_task(client, key_a, task["id"], session_a["id"])).json()
    run_a = (await _start_run(client, key_a, task["id"], claim_a)).json()

    # Claim A expires; B takes over.
    backdate_expiry(sync_engine, "task_claims", claim_a["id"])
    claim_b = (await claim_task(client, key_b, task["id"], session_b["id"])).json()
    assert claim_b["fencingToken"] == claim_a["fencingToken"] + 1

    # Zombie run A wakes up and tries to write the final result -> rejected.
    response = await client.post(
        f"/api/v1/runs/{run_a['id']}:succeed",
        json={"output": {"pr": 1}},
        headers=auth(key_a),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_claim"
    task_now = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert task_now["status"] == "in_progress"

    # B starts its own run: zombie run A is superseded (failed) automatically.
    run_b = (await _start_run(client, key_b, task["id"], claim_b)).json()
    assert run_b["attempt"] == 2
    run_a_after = (await client.get(f"/api/v1/runs/{run_a['id']}", headers=auth(admin_key))).json()
    assert run_a_after["status"] == "failed"
    assert run_a_after["failureReason"] == "superseded"

    # B finishes: task completed; history of both attempts remains.
    response = await client.post(f"/api/v1/runs/{run_b['id']}:succeed", headers=auth(key_b))
    assert response.status_code == 200
    assert response.json()["task"]["status"] == "done"
    runs = (await client.get(f"/api/v1/runs?taskId={task['id']}", headers=auth(admin_key))).json()[
        "items"
    ]
    assert {r["attempt"]: r["status"] for r in runs} == {1: "failed", 2: "succeeded"}


async def test_run_cancel_and_holder_checks(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, key_a, session_a = await _setup(client, admin_key, "agent-a")
    _, key_b, _ = await _setup(client, admin_key, "agent-b")
    task = await create_task(client, admin_key)
    claim = (await claim_task(client, key_a, task["id"], session_a["id"])).json()
    run = (await _start_run(client, key_a, task["id"], claim)).json()

    # Another principal cannot finish someone else's run...
    response = await client.post(f"/api/v1/runs/{run['id']}:fail", headers=auth(key_b))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "run_holder_mismatch"

    # ...but an admin (claims.manage) can cancel it.
    response = await client.post(
        f"/api/v1/runs/{run['id']}:cancel",
        json={"reason": "operator abort"},
        headers=auth(admin_key),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


async def test_succeed_response_task_matches_the_get_projection(
    client: httpx.AsyncClient,
) -> None:
    """Regression (TASK-000048): the :succeed response used to dump the task
    row raw, skipping the projectId/typeKey/typeVersion resolved from related
    tables (ADR-0035, ADR-0048). Both must come from the same builder."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key, session = await _setup(client, admin_key)

    response = await client.post(
        "/api/v1/task-types",
        json={"key": "incident", "displayName": "Incident"},
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text

    # A project needs a template: it is what supplies the profile the project
    # is created under (ADR-0048), so there is no "just a project" to create.
    response = await client.post(
        "/api/v1/project-templates",
        json={"key": "delivery", "displayName": "Delivery"},
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text

    workspace = await create_workspace(client, admin_key, "proj-ws")
    response = await client.post(
        "/api/v1/projects",
        json={"workspaceId": workspace["id"], "templateKey": "delivery"},
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text
    project = response.json()

    task = await create_task(client, admin_key, workspaceId=workspace["id"], typeKey="incident")
    claim = (await claim_task(client, agent_key, task["id"], session["id"])).json()
    run = (await _start_run(client, agent_key, task["id"], claim)).json()

    response = await client.post(
        f"/api/v1/runs/{run['id']}:succeed",
        json={"output": {}},
        headers=auth(agent_key),
    )
    assert response.status_code == 200, response.text
    completed_task = response.json()["task"]

    fetched = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(agent_key))).json()

    assert completed_task["projectId"] == fetched["projectId"] == project["id"]
    assert completed_task["typeKey"] == fetched["typeKey"] == "incident"
    assert completed_task["typeVersion"] == fetched["typeVersion"] == 1
