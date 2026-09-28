"""``agent:<key>`` in an assignment field (CP-ADR-0073, amendment A1; declarative-cycle C006).

What is under test: the reference is resolved when the task is written — in
``POST``/``PATCH /tasks``, an approval outcome's ``ensureWork`` and a rule's
``ensure_work`` — to the principal the core derived for the agent, and the
task stores and returns that id. An agent the tenant does not have, one that
is retired and one with no identity yet are refused with ``422
unknown_agent`` naming the field and the key, and nothing is written.
"""

from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.worker.main import Worker
from tests.helpers import auth, create_task, do_bootstrap
from tests.integration.test_agent_registry import _link, _publish, _tenant, coder_spec
from tests.integration.test_approval_outcomes import _decide, _outcome, _review_setup, worker

__all__ = ["worker"]

AGENT = "fixer"
REFERENCE = f"agent:{AGENT}"


async def _agent(client: httpx.AsyncClient, *, linked: bool = True) -> dict[str, Any]:
    """A tenant with the agent ``fixer`` published and, unless asked not to, linked."""
    admin_key, workspace = await _tenant(client)
    published = await _publish(client, admin_key, coder_spec(workspace["id"]), agent=AGENT)
    assert published.status_code == 201, published.text
    principal_id = None
    if linked:
        response = await _link(client, admin_key, agent=AGENT)
        assert response.status_code == 200, response.text
        principal_id = response.json()["principalId"]
    return {"key": admin_key, "workspace": workspace, "principal": principal_id}


def _unknown(response: httpx.Response, field: str, agent: str = AGENT) -> None:
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "unknown_agent"
    assert error["details"] == {"field": field, "agent": agent}


def _task_count(sync_engine: Engine) -> int:
    with sync_engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM tasks")).scalar_one())


# --- POST / PATCH /tasks -------------------------------------------------------------


async def test_a_task_is_assigned_to_the_agent_s_principal(client: httpx.AsyncClient) -> None:
    s = await _agent(client)

    created = await client.post(
        "/api/v1/tasks", json={"title": "Sample", "assigneeId": REFERENCE}, headers=auth(s["key"])
    )
    assert created.status_code == 201, created.text
    # The id is stored and returned, never the reference.
    assert created.json()["assigneeId"] == s["principal"]

    other = await create_task(client, s["key"], title="Unassigned")
    patched = await client.patch(
        f"/api/v1/tasks/{other['id']}",
        json={"assigneeId": REFERENCE},
        headers={**auth(s["key"]), "If-Match": f'"task-{other["version"]}"'},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["assigneeId"] == s["principal"]
    # The queue of the agent filters by the id, as for any assignee.
    listed = await client.get(
        "/api/v1/tasks", params={"assigneeId": s["principal"]}, headers=auth(s["key"])
    )
    assert {t["id"] for t in listed.json()["items"]} == {created.json()["id"], other["id"]}


async def test_an_unknown_agent_is_refused_and_nothing_is_written(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    before = _task_count(sync_engine)

    _unknown(
        await client.post(
            "/api/v1/tasks",
            json={"title": "Sample", "assigneeId": "agent:nobody"},
            headers=auth(key),
        ),
        "assigneeId",
        agent="nobody",
    )
    assert _task_count(sync_engine) == before

    task = await create_task(client, key, title="Sample")
    _unknown(
        await client.patch(
            f"/api/v1/tasks/{task['id']}",
            json={"assigneeId": "agent:nobody", "title": "Renamed"},
            headers={**auth(key), "If-Match": f'"task-{task["version"]}"'},
        ),
        "assigneeId",
        agent="nobody",
    )
    after = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(key))).json()
    assert (after["version"], after["title"]) == (task["version"], task["title"])


async def test_an_agent_without_an_identity_has_nobody_to_assign(
    client: httpx.AsyncClient,
) -> None:
    s = await _agent(client, linked=False)
    _unknown(
        await client.post(
            "/api/v1/tasks",
            json={"title": "Sample", "assigneeId": REFERENCE},
            headers=auth(s["key"]),
        ),
        "assigneeId",
    )


async def test_a_retired_agent_is_not_assigned(client: httpx.AsyncClient) -> None:
    s = await _agent(client)
    retired = await client.post(
        f"/api/v1/agents/{AGENT}:retire", json={"reason": "gone"}, headers=auth(s["key"])
    )
    assert retired.status_code == 200, retired.text

    _unknown(
        await client.post(
            "/api/v1/tasks",
            json={"title": "Sample", "assigneeId": REFERENCE},
            headers=auth(s["key"]),
        ),
        "assigneeId",
    )


async def test_the_reference_is_resolved_only_for_a_writer(client: httpx.AsyncClient) -> None:
    """Without ``tasks.write`` the answer is 403, not whether the agent exists."""
    from tests.helpers import create_agent_with_key

    s = await _agent(client)
    _, reader_key = await create_agent_with_key(
        client, s["key"], name="reader", permissions=["tasks.read"]
    )
    response = await client.post(
        "/api/v1/tasks", json={"title": "Sample", "assigneeId": REFERENCE}, headers=auth(reader_key)
    )
    assert response.status_code == 403, response.text


# --- ensureWork of an approval outcome ----------------------------------------------


def _fix_schema(assignee: str) -> dict[str, Any]:
    ensure = {
        "ensureWork": {
            "type": "coding-task",
            "key": "fix:$.approval.id",
            "title": "Fix $.spawnedBy.publicId",
            "assignee": assignee,
        }
    }
    return {"gates": {"default": {"outcomes": {"rejected": [ensure, {"completeTask": {}}]}}}}


async def test_ensure_work_of_an_outcome_assigns_the_agent(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _agent(client)
    review = await _review_setup(client, schema=_fix_schema(REFERENCE), admin_key=s["key"])
    approval_id = review["approval"]["id"]

    await _decide(client, review["reviewer_key"], approval_id, "reject", "Redo")
    await worker.run_once()

    outcome = await _outcome(client, s["key"], approval_id)
    assert outcome["outcomeStatus"] == "executed", outcome
    filed = outcome["actions"][0]["result"]
    task = (await client.get(f"/api/v1/tasks/{filed['taskId']}", headers=auth(s["key"]))).json()
    assert task["assigneeId"] == s["principal"]


async def test_ensure_work_of_an_outcome_fails_on_an_unknown_agent(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _agent(client)
    review = await _review_setup(client, schema=_fix_schema("agent:nobody"), admin_key=s["key"])
    approval_id = review["approval"]["id"]

    await _decide(client, review["reviewer_key"], approval_id, "reject", "Redo")
    await worker.run_once()

    outcome = await _outcome(client, s["key"], approval_id)
    assert outcome["outcomeStatus"] == "failed"
    assert [(a["action"], a["status"]) for a in outcome["actions"]] == [
        ("ensureWork", "failed"),
        ("completeTask", "not_executed"),
    ]
    error = outcome["actions"][0]["error"]
    assert error["code"] == "unknown_agent"
    assert error["details"] == {"field": "ensureWork.assignee", "agent": "nobody"}


# --- ensure_work of a rule ------------------------------------------------------------


def _rule(assignee: str) -> dict[str, Any]:
    return {
        "key": "sample-appeared",
        "trigger": {"kind": "observation", "type": "sample.appeared"},
        "action": {
            "kind": "ensure_work",
            "taskType": "task",
            "dedupKeyTemplate": "sample:{{payload.data.id}}",
            "fields": {"title": "Sample {{payload.data.id}}", "assignee": assignee},
        },
    }


async def _evaluate(
    client: httpx.AsyncClient, worker: Worker, key: str, assignee: str
) -> dict[str, Any]:
    rule = await client.post("/api/v1/rules", json=_rule(assignee), headers=auth(key))
    assert rule.status_code == 201, rule.text
    observed = await client.post(
        "/api/v1/observations",
        json={"kind": "sample.appeared", "content": "seen", "data": {"id": "a"}},
        headers=auth(key),
    )
    assert observed.status_code in (200, 201), observed.text
    await worker.run_once()
    evaluations = await client.get(
        f"/api/v1/rules/{rule.json()['id']}/evaluations", headers=auth(key)
    )
    [evaluation] = evaluations.json()["items"]
    result: dict[str, Any] = evaluation
    return result


async def test_ensure_work_of_a_rule_assigns_the_agent(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _agent(client)
    evaluation = await _evaluate(client, worker, s["key"], REFERENCE)

    assert evaluation["status"] == "matched", evaluation
    [work] = evaluation["result"]["work"]
    task = (await client.get(f"/api/v1/tasks/{work['taskId']}", headers=auth(s["key"]))).json()
    assert task["assigneeId"] == s["principal"]


async def test_ensure_work_of_a_rule_fails_on_an_unknown_agent(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _agent(client)
    before = _task_count(sync_engine)
    evaluation = await _evaluate(client, worker, s["key"], "agent:nobody")

    assert evaluation["status"] == "failed"
    assert evaluation["error"]["code"] == "unknown_agent"
    assert evaluation["error"]["details"] == {"field": "action.fields.assignee", "agent": "nobody"}
    assert _task_count(sync_engine) == before
