"""Checks of a task type, ``when``, an external write after a decision (C004).

CP-ADR-0067, amendment 2026-09-27 (B5-B8). A task type version declares
checks every task of it passes: after the required outputs, before the task's
own acceptance, never replaced by it. A check with ``when`` is ``skipped``
when the task lacks what the condition reads, so work without a result does
not wait for a write of it. A ``deterministic`` check whose skill writes
outside runs only after a person's decision passed in the same attempt, with
the decider's authority and the decision as its basis. A rejected decision or
a failed write returns the task to its executor; its dependents wait for the
attempt all along. Skills, types and artifacts are named neutrally.
"""

from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from control_plane.worker.main import Worker
from tests.helpers import auth, create_agent_with_key, create_task
from tests.integration.test_verification_m16 import (
    _answer,
    _approval,
    _complete,
    _decide,
    _events,
    _make_due,
    _publish,
    _setup,
    _task,
    _verifications,
    worker,
)

__all__ = ["worker"]

WRITER = "write.sample"
TYPE_KEY = "sample-work"
PUBLISHED = "$.task.artifact[result].metadata.published"
WRITE = {
    "key": "write-out",
    "kind": "deterministic",
    "description": "The result is written out",
    "spec": {
        "skill": f"{WRITER}@1",
        "inputs": {"subject": "$.task.publicId"},
        "expect": {"ok": True},
    },
    "when": [PUBLISHED],
}
# A task's own check under the key of one of its type's.
MINE = {"key": "write-out", "kind": "external_state", "description": "Seen written"}
# Whoever decides also makes the write: these are the rights it needs.
DECIDER_PERMISSIONS = [
    "approvals.decide",
    "approvals.read",
    "tasks.read",
    "tasks.write",
    "skills.invoke",
]


def _review(decider: str) -> dict[str, Any]:
    return {
        "key": "review",
        "kind": "human",
        "description": "A person approves the result",
        "spec": {"approver": decider},
        "when": [PUBLISHED],
    }


async def _type(
    client: httpx.AsyncClient, key: str, acceptance: list[dict[str, Any]]
) -> httpx.Response:
    return await client.post(
        "/api/v1/task-types",
        json={
            "key": TYPE_KEY,
            "displayName": "Sample work",
            "lifecycleSchema": SYSTEM_TASK_LIFECYCLE,
            "acceptance": acceptance,
        },
        headers=auth(key),
    )


async def _cycle(client: httpx.AsyncClient) -> dict[str, Any]:
    """A tenant with a writing skill, a decider and a type that reviews, then writes."""
    s = await _setup(client)
    key = s["admin_key"]
    await _publish(client, key, name=WRITER, side_effects="external_write")
    decider, decider_key = await create_agent_with_key(
        client, key, name="decider", permissions=DECIDER_PERMISSIONS
    )
    runner, _ = await create_agent_with_key(client, key, name="runner")
    created = await _type(client, key, [_review(decider["id"]), WRITE])
    assert created.status_code == 201, created.text
    return {
        **s,
        "decider": decider["id"],
        "decider_key": decider_key,
        "runner": runner["id"],
        "type": created.json(),
    }


async def _result(client: httpx.AsyncClient, key: str, task_id: str) -> None:
    response = await client.post(
        "/api/v1/artifacts",
        json={"type": "result", "name": "result", "task": task_id, "metadata": {"published": True}},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text


async def _claimability(client: httpx.AsyncClient, key: str, ref: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{ref}/claimability", headers=auth(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _reasons(claimability: dict[str, Any]) -> set[str]:
    return {reason["code"] for reason in claimability["reasons"]}


def _invocation(sync_engine: Engine, invocation_id: str) -> Any:
    with sync_engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT authority_principal_id, authorization_basis, requested_by_kind "
                "FROM skill_invocations WHERE id = :id"
            ),
            {"id": invocation_id},
        ).one()


def _task_count(sync_engine: Engine) -> int:
    with sync_engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM tasks")).scalar_one())


# --- publication ------------------------------------------------------------------


async def test_a_type_publishes_its_checks_and_refuses_those_nobody_could_run(
    client: httpx.AsyncClient,
) -> None:
    c = await _cycle(client)
    key = c["admin_key"]
    review = _review(c["decider"])
    assert [check["key"] for check in c["type"]["acceptance"]] == ["review", "write-out"]
    assert c["type"]["acceptance"][1]["when"] == [PUBLISHED]
    stored = await client.get(f"/api/v1/task-types/{c['type']['id']}", headers=auth(key))
    assert stored.json()["acceptance"] == c["type"]["acceptance"]

    refused = [
        # A write with no decision before it, or one under another condition.
        ([WRITE], "acceptance[0].spec.skill", "invalid_acceptance_spec"),
        ([{**review, "when": ["$.task.status"]}, WRITE], "acceptance[1].spec.skill", None),
        ([WRITE, review], "acceptance[0].spec.skill", "invalid_acceptance_spec"),
        ([{**review, "when": ["$.approval.comment"]}], "acceptance[0].when[0]", None),
        ([{**review, "key": "output.report"}], "acceptance[0].key", "invalid_acceptance"),
        ([{**WRITE, "spec": {"skill": "missing.sample@1"}}], "acceptance[0].spec.skill", None),
    ]
    for acceptance, field, code in refused:
        response = await _type(client, key, acceptance)
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["details"]["field"] == field
        assert error["code"] == (code or "invalid_acceptance_spec")
    no_decision = (await _type(client, key, [WRITE])).json()["error"]["details"]
    assert no_decision["cause"] == "external_write_without_decision"
    versions = await client.get(f"/api/v1/task-types?key={TYPE_KEY}", headers=auth(key))
    assert len(versions.json()["items"]) == 1


async def test_a_task_adds_checks_to_its_type_but_never_replaces_one(
    client: httpx.AsyncClient,
) -> None:
    c = await _cycle(client)
    key = c["admin_key"]
    replaced = await client.post(
        "/api/v1/tasks",
        json={"title": "Mine", "typeKey": TYPE_KEY, "acceptance": [MINE]},
        headers=auth(key),
    )
    assert replaced.status_code == 422, replaced.text
    assert replaced.json()["error"]["code"] == "invalid_acceptance"
    assert replaced.json()["error"]["details"]["field"] == "acceptance[0].key"

    # The task's own write rests on the decision its type declares, same condition.
    own_write = {**WRITE, "key": "write-copy"}
    task = await create_task(client, key, title="Mine", typeKey=TYPE_KEY, acceptance=[own_write])
    # The task keeps its own document; the type's checks are read from the type.
    assert [check["key"] for check in task["acceptance"]] == ["write-copy"]
    unconditional = {key: value for key, value in own_write.items() if key != "when"}
    response = await client.post(
        "/api/v1/tasks",
        json={"title": "Mine", "typeKey": TYPE_KEY, "acceptance": [unconditional]},
        headers=auth(key),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["cause"] == "external_write_without_decision"

    # Evidence may cite a check of the type.
    cited = await client.post(
        "/api/v1/tasks",
        json={
            "title": "Cited",
            "typeKey": TYPE_KEY,
            "evidence": [
                {"kind": "external", "externalRef": {"system": "s", "id": "1"}, "check": "review"}
            ],
        },
        headers=auth(key),
    )
    assert cited.status_code == 201, cited.text


# --- when -------------------------------------------------------------------------


async def test_a_task_without_the_result_skips_the_conditional_checks(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    c = await _cycle(client)
    key = c["admin_key"]
    task = await create_task(client, key, title="Nothing to write", typeKey=TYPE_KEY)
    assert (await _complete(client, key, task["id"])).status_code == 200
    await worker.run_once()

    attempt = (await _verifications(client, key, task["id"]))[0]
    assert attempt["status"] == "passed"
    assert [(c["key"], c["source"]) for c in attempt["checks"]] == [
        ("review", "type"),
        ("write-out", "type"),
    ]
    assert [
        (r["key"], r["source"], r["status"], r["reason"], r["details"]) for r in attempt["results"]
    ] == [
        ("review", "type", "skipped", "condition_unmet", {"when": PUBLISHED}),
        ("write-out", "type", "skipped", "condition_unmet", {"when": PUBLISHED}),
    ]
    assert (await _task(client, key, task["id"]))["status"] == "done"
    verified = _events(sync_engine, "task.verified", task["id"])
    assert [r["status"] for r in verified[0]["results"]] == ["skipped", "skipped"]
    with sync_engine.connect() as conn:
        asked = conn.execute(
            text("SELECT count(*) FROM approvals WHERE task_id = :task"), {"task": task["id"]}
        ).scalar_one()
    assert asked == 0


# --- the write after the decision ---------------------------------------------------


async def test_a_dependent_waits_until_the_decision_and_the_write_pass(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    c = await _cycle(client)
    key = c["admin_key"]
    first = await create_task(client, key, title="First", typeKey=TYPE_KEY)
    second = await create_task(client, key, title="Second")
    related = await client.post(
        f"/api/v1/tasks/{second['id']}/relations",
        json={"toTask": first["id"], "type": "depends_on"},
        headers=auth(key),
    )
    assert related.status_code == 201, related.text
    await _result(client, key, first["id"])
    assert (await _complete(client, key, first["id"])).status_code == 200
    await worker.run_once()

    # Handed in, waiting for the decision: neither it nor its dependent is taken.
    attempt = (await _verifications(client, key, first["id"]))[0]
    assert attempt["status"] == "waiting_human"
    assert "verification_pending" in _reasons(await _claimability(client, key, first["id"]))
    assert "task_not_ready" in _reasons(await _claimability(client, key, second["id"]))
    approval = await _approval(client, key, attempt["approvalId"])
    assert approval["assignedPrincipalId"] == c["decider"]

    await _decide(client, c["decider_key"], approval["id"], "approve")
    await worker.run_once()
    attempt = (await _verifications(client, key, first["id"]))[0]
    assert attempt["status"] == "running", attempt["results"]
    assert attempt["results"][0]["status"] == "passed"
    # The write is the decider's, on the decision: not the completer's.
    call = _invocation(sync_engine, attempt["skillInvocationId"])
    assert str(call.authority_principal_id) == c["decider"]
    assert call.authorization_basis["kind"] == "approval"
    assert call.authorization_basis["approvalId"] == approval["id"]
    assert call.requested_by_kind == "verification"
    assert "task_not_ready" in _reasons(await _claimability(client, key, second["id"]))

    await _answer(client, c["executor_key"], {"ok": True})
    _make_due(sync_engine)
    await worker.run_once()
    attempt = (await _verifications(client, key, first["id"]))[0]
    assert attempt["status"] == "passed"
    assert [r["status"] for r in attempt["results"]] == ["passed", "passed"]
    assert (await _task(client, key, first["id"]))["status"] == "done"
    assert "task_not_ready" not in _reasons(await _claimability(client, key, second["id"]))


async def test_a_rejected_decision_returns_the_task_to_its_executor(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    c = await _cycle(client)
    key = c["admin_key"]
    task = await create_task(client, key, title="Not yet", typeKey=TYPE_KEY, assigneeId=c["runner"])
    await _result(client, key, task["id"])
    tasks_before = _task_count(sync_engine)
    assert (await _complete(client, key, task["id"])).status_code == 200
    await worker.run_once()
    approval_id = (await _verifications(client, key, task["id"]))[0]["approvalId"]

    await _decide(client, c["decider_key"], approval_id, "reject", comment="Rename the result")
    await worker.run_once()
    attempt = (await _verifications(client, key, task["id"]))[0]
    assert attempt["status"] == "failed"
    assert [(r["key"], r["status"], r["reason"]) for r in attempt["results"]] == [
        ("review", "failed", "approval_rejected")
    ]
    current = await _task(client, key, task["id"])
    assert (current["status"], current["assigneeId"]) == ("todo", c["runner"])
    assert "verification_pending" not in _reasons(await _claimability(client, key, task["id"]))
    comments = (await client.get(f"/api/v1/tasks/{task['id']}/comments", headers=auth(key))).json()
    assert "Rename the result" in comments["items"][0]["body"]
    # Nothing written, no task of fixes filed.
    with sync_engine.connect() as conn:
        calls = conn.execute(
            text("SELECT count(*) FROM skill_invocations WHERE task_id = :task"),
            {"task": task["id"]},
        ).scalar_one()
    assert calls == 0
    assert _task_count(sync_engine) == tasks_before


async def test_a_failed_write_returns_the_task_and_the_next_attempt_asks_again(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    c = await _cycle(client)
    key = c["admin_key"]
    task = await create_task(
        client, key, title="Conflict", typeKey=TYPE_KEY, assigneeId=c["runner"]
    )
    await _result(client, key, task["id"])
    assert (await _complete(client, key, task["id"])).status_code == 200
    await worker.run_once()
    first = (await _verifications(client, key, task["id"]))[0]["approvalId"]
    await _decide(client, c["decider_key"], first, "approve")
    await worker.run_once()
    await _answer(client, c["executor_key"], {"ok": False})
    _make_due(sync_engine)
    await worker.run_once()

    attempt = (await _verifications(client, key, task["id"]))[0]
    assert attempt["status"] == "failed"
    assert [(r["status"], r["reason"]) for r in attempt["results"]] == [
        ("passed", None),
        ("failed", "expectation_not_met"),
    ]
    current = await _task(client, key, task["id"])
    assert (current["status"], current["assigneeId"]) == ("todo", c["runner"])

    # Handed in again: a new attempt and a new decision, the old one is spent.
    moved = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "in_progress"},
        headers={**auth(key), "If-Match": f'"task-{current["version"]}"'},
    )
    assert moved.status_code == 200, moved.text
    assert (await _complete(client, key, task["id"])).status_code == 200
    await worker.run_once()
    again = (await _verifications(client, key, task["id"]))[0]
    assert (again["attempt"], again["status"]) == (2, "waiting_human")
    assert again["approvalId"] not in (None, first)
