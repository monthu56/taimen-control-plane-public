"""Process instances at work on Postgres (CP-ADR-0074 §3-§8; process-packages P009).

The acceptance of P009: an observation starts an instance, whose human step
files a task of the core; the task's completion moves the instance on; a
change of the data moves the deadline of the next step's timers; the timers
fire, escalate and close the case. The same start event again reaches the
same instance (``process.correlated``), never a second one.

Everything runs through the public API and the worker, as in production:
the instance acts as its identity agent, its facts come back from the
journal through the worker's own cursor, its timers are rows the worker's
timer loop picks up. Observation kinds and task types are neutral
(``sample.*``): the core knows no domain.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from control_plane.worker.main import Worker
from tests.helpers import auth, create_agent_with_key, do_bootstrap

AGENT = "sample-process"
ISSUER = "https://iam.example.test"
AGENT_PERMISSIONS = [
    "approvals.manage",
    "events.read",
    "skills.invoke",
    "tasks.read",
    "tasks.write",
]


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


def _case(admin: str) -> dict[str, Any]:
    """A case: a review, then a signature due by a deadline the case may move.

    The signature escalates at its deadline (``notify``) and a second after it
    (``raise``); the raise is caught and closes the case as ``overdue``.
    """
    person = [{"principal": admin}]
    return {
        "version": 1,
        "displayName": "Sample case",
        "identity": {"agent": AGENT},
        "owner": [{"role": "lead"}],
        "data": {
            "type": "object",
            "properties": {
                "number": {"type": "string"},
                "deadline": {"type": "string", "format": "date-time"},
                "decision": {"type": "string"},
            },
        },
        "start": {
            "on": {"observation": "sample.opened"},
            "key": "event.payload.data.number",
            "set": {
                "number": "string(event.payload.data.number)",
                "deadline": "timestamp(event.payload.data.deadline)",
            },
        },
        "correlate": [
            {
                "on": {"observation": "sample.moved"},
                "key": "event.payload.data.number",
                "set": {"deadline": "timestamp(event.payload.data.deadline)"},
            }
        ],
        "stages": [
            {
                "id": "work",
                "steps": [
                    {
                        "id": "review",
                        "human": {"taskType": "review", "assign": person},
                        "output": {"as": {"decision": "step.result.decision"}},
                    },
                    {
                        "id": "guard",
                        "try": {
                            "do": [
                                {
                                    "id": "sign",
                                    "human": {
                                        "taskType": "review",
                                        "assign": person,
                                        "due": {"at": "data.deadline"},
                                        "escalations": [
                                            {"after": "due", "action": "notify", "to": person},
                                            {
                                                "after": "PT1S",
                                                "action": "raise",
                                                "error": {"type": "overdue"},
                                            },
                                        ],
                                    },
                                }
                            ],
                            "catch": [
                                {
                                    "errors": {"type": "overdue"},
                                    "do": [
                                        {"id": "closed-overdue", "complete": {"outcome": "overdue"}}
                                    ],
                                }
                            ],
                        },
                    },
                    {"id": "done", "complete": {"outcome": "signed"}},
                ],
            }
        ],
    }


# A standing goal (TAI-ADR-0055): no complete; its milestone follows the data.
GOAL: dict[str, Any] = {
    "version": 1,
    "displayName": "Sample goal",
    "identity": {"agent": AGENT},
    "owner": [{"role": "lead"}],
    "data": {"type": "object", "properties": {"open": {"type": "integer"}}},
    "start": {"on": {"observation": "sample.goal"}, "key": "event.payload.data.goal"},
    "correlate": [
        {
            "on": {"observation": "sample.count"},
            "key": "event.payload.data.goal",
            "set": {"open": "int(event.payload.data.open)"},
        }
    ],
    "stages": [
        {
            "id": "watch",
            "milestones": [{"id": "clear", "when": "has(data.open) && data.open == 0"}],
            "steps": [
                {"id": "hold", "listen": {"any": [{"on": {"observation": "sample.never"}}]}},
            ],
        }
    ],
}


async def _setup(client: httpx.AsyncClient) -> dict[str, Any]:
    boot = await do_bootstrap(client)
    key: str = boot["apiKey"]["key"]
    responses = [
        await client.post(
            "/api/v1/agents",
            json={
                "key": AGENT,
                "spec": {
                    "displayName": "Sample process",
                    "identity": {"kind": "service", "permissions": AGENT_PERMISSIONS},
                    "placement": "none",
                },
            },
            headers=auth(key),
        ),
        await client.post(
            "/api/v1/task-types",
            json={
                "key": "review",
                "displayName": "Review",
                "lifecycleSchema": SYSTEM_TASK_LIFECYCLE,
                "fieldSchema": {"type": "object", "properties": {"decision": {"type": "string"}}},
            },
            headers=auth(key),
        ),
    ]
    for response in responses:
        assert response.status_code in (200, 201), response.text
    principal = await _link_agent(client, key)
    return {"key": key, "admin": boot["adminPrincipal"]["id"], "agent": principal}


async def _link_agent(client: httpx.AsyncClient, admin_key: str) -> str:
    _, fleet_key = await create_agent_with_key(
        client,
        admin_key,
        name=f"fleet-{uuid.uuid4().hex[:6]}",
        kind="service",
        permissions=["agents.read", "agents.status.write"],
    )
    response = await client.put(
        f"/api/v1/agents/{AGENT}/identity",
        json={
            "issuer": ISSUER,
            "iamTenantId": str(uuid.uuid4()),
            "iamPrincipalId": str(uuid.uuid4()),
        },
        headers=auth(fleet_key),
    )
    assert response.status_code == 200, response.text
    principal_id: str = response.json()["principalId"]
    return principal_id


async def _publish(client: httpx.AsyncClient, key: str, process: str, spec: dict[str, Any]) -> None:
    response = await client.post(
        "/api/v1/process-definitions", json={"key": process, "spec": spec}, headers=auth(key)
    )
    assert response.status_code == 201, response.text


async def _observe(client: httpx.AsyncClient, key: str, kind: str, **data: Any) -> None:
    response = await client.post(
        "/api/v1/observations",
        json={"kind": kind, "content": f"{kind} seen", "data": data},
        headers=auth(key),
    )
    assert response.status_code in (200, 201), response.text


async def _events(client: httpx.AsyncClient, key: str, event_type: str) -> list[dict[str, Any]]:
    response = await client.get(
        "/api/v1/events", params={"types": event_type, "limit": 200}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def _instances(client: httpx.AsyncClient, key: str, **params: Any) -> list[dict[str, Any]]:
    response = await client.get("/api/v1/process-instances", params=params, headers=auth(key))
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def _instance(client: httpx.AsyncClient, key: str, instance_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/process-instances/{instance_id}", headers=auth(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _journal(
    client: httpx.AsyncClient, key: str, instance_id: str, **params: Any
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = None
    while True:
        query = {**params, "limit": 50, **({"cursor": cursor} if cursor else {})}
        response = await client.get(
            f"/api/v1/process-instances/{instance_id}/journal", params=query, headers=auth(key)
        )
        assert response.status_code == 200, response.text
        page = response.json()
        items += page["items"]
        cursor = page.get("nextCursor")
        if not cursor:
            return items


async def _task(client: httpx.AsyncClient, key: str, task_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{task_id}", headers=auth(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _complete(
    client: httpx.AsyncClient, key: str, task_id: str, fields: dict[str, Any]
) -> None:
    task = await _task(client, key, task_id)
    patched = await client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"customFields": fields},
        headers={**auth(key), "If-Match": f'"task-{task["version"]}"'},
    )
    assert patched.status_code == 200, patched.text
    done = await client.post(
        f"/api/v1/tasks/{task_id}:complete",
        headers={**auth(key), "If-Match": f'"task-{patched.json()["version"]}"'},
    )
    assert done.status_code == 200, done.text


def _open(instance: dict[str, Any], element: str) -> dict[str, Any]:
    [found] = [e for e in instance["openElements"] if e["id"] == element]
    return found


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _references(sync_engine: Engine, task_id: str) -> list[str]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT external_id FROM external_references"
                " WHERE entity_type = 'task' AND entity_id = :task"
            ),
            {"task": task_id},
        ).all()
    return [row.external_id for row in rows]


# --- the acceptance of P009 --------------------------------------------------------


async def test_start_task_completion_timer_escalation_and_close(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, "sample-case", _case(s["admin"]))
    deadline = datetime.now(UTC) + timedelta(days=30)

    # Start: the observation opens the case; its human step is a task of the core.
    await _observe(client, key, "sample.opened", number="S-1", deadline=deadline.isoformat())
    await worker.run_once()
    [instance] = await _instances(client, key, definitionKey="sample-case")
    assert (instance["instanceKey"], instance["status"], instance["definitionVersion"]) == (
        "S-1",
        "running",
        1,
    )
    assert instance["data"]["number"] == "S-1"
    review = _open(instance, "review")
    assert review["kind"] == "human"
    task = await _task(client, key, review["taskId"])
    assert task["assigneeId"] == s["admin"]
    assert task["goalId"] is None
    assert task["origin"]["kind"] == "process"
    assert task["origin"]["ref"] == f"process/{instance['id']}/review"
    assert _references(sync_engine, review["taskId"]) == [f"process/{instance['id']}/review"]
    [created] = [
        e for e in await _events(client, key, "task.created") if e["entityId"] == task["id"]
    ]
    assert created["actorId"] == s["agent"], "the process acts as its identity agent"
    [started] = await _events(client, key, "process.started")
    assert started["entityId"] == instance["id"]
    assert started["actorId"] == s["agent"]
    assert started["payload"]["triggerType"] == "observation:sample.opened"

    # The start event again: the same instance, correlated.
    await _observe(client, key, "sample.opened", number="S-1", deadline=deadline.isoformat())
    await worker.run_once()
    assert len(await _instances(client, key, definitionKey="sample-case")) == 1
    [correlated] = await _events(client, key, "process.correlated")
    assert correlated["payload"]["instanceKey"] == "S-1"

    # Completion: the task's fields become data, the next step waits with timers.
    await _complete(client, key, review["taskId"], {"decision": "yes"})
    await worker.run_once()
    instance = await _instance(client, key, instance["id"])
    assert instance["data"]["decision"] == "yes"
    sign = _open(instance, "sign")
    assert sign["taskId"] is not None
    first, second = sorted(_time(t["dueAt"]) for t in instance["timers"] if t["element"] == "sign")
    assert abs(first - deadline) < timedelta(seconds=1), "notify at the deadline"
    assert second - first == timedelta(seconds=1), "raise a second after it"
    assert all(t["state"] == "pending" for t in instance["timers"])

    # A change of the data moves the timers that read it: the deadline is past now.
    moved = datetime.now(UTC) - timedelta(hours=1)
    await _observe(client, key, "sample.moved", number="S-1", deadline=moved.isoformat())
    await worker.run_once()
    rescheduled = await _events(client, key, "process.timer_rescheduled")
    assert len(rescheduled) == 2
    assert {e["payload"]["cause"] for e in rescheduled} == {"data_changed"}
    assert all(e["payload"]["changedFields"] == ["deadline"] for e in rescheduled)
    assert all(
        abs(_time(e["payload"]["dueAt"]) - moved) < timedelta(seconds=2) for e in rescheduled
    )

    # The timers fire: notify at the deadline, raise a second later — caught, closed.
    await worker.run_once()
    fired = await _events(client, key, "process.timer_fired")
    assert len(fired) == 2
    escalated = await _events(client, key, "process.escalated")
    assert [(e["payload"]["level"], e["payload"]["action"]) for e in escalated] == [
        (1, "notify"),
        (2, "raise"),
    ]
    instance = await _instance(client, key, instance["id"])
    assert (instance["status"], instance["outcome"]) == ("completed", "overdue")
    assert instance["completedAt"] is not None
    assert instance["openElements"] == []
    assert instance["timers"] == []
    [completed] = await _events(client, key, "process.completed")
    assert completed["payload"]["outcome"] == "overdue"
    # The signature nobody gave is cancelled by the process, not left open.
    assert (await _task(client, key, sign["taskId"]))["systemStatusCategory"] == (
        "terminal_cancelled"
    )

    # The decision journal: every input with its decisions and their reasons.
    journal = await _journal(client, key, instance["id"])
    inputs = [e for e in journal if e["kind"] == "input"]
    assert [e["data"]["input"]["kind"] for e in inputs] == [
        "start",
        "start",
        "task",
        "event",
        "timer",
        "timer",
    ]
    assert [e["seq"] for e in inputs] == [0, 1, 2, 3, 4, 5]
    assert all(e["eventId"] for e in inputs[:4])
    timers = await _journal(client, key, instance["id"], kind="timer")
    assert {e["data"]["decision"] for e in timers} >= {
        "timer_set",
        "timer_rescheduled",
        "timer_fired",
        "escalated",
    }
    errors = await _journal(client, key, instance["id"], kind="error")
    assert any(e["data"]["decision"] == "error_raised" for e in errors)

    # A redelivered batch changes nothing: every input was taken once.
    written = len(await _events(client, key, "process.correlated"))
    assert written == 2, "the repeated start and the moved deadline"
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE event_consumer_cursors SET tx_id = 0, sequence = 0 WHERE name = :n"),
            {"n": "processes"},
        )
    await worker.run_once()
    assert len(await _journal(client, key, instance["id"], kind="input")) == len(inputs)
    assert len(await _events(client, key, "process.correlated")) == written


async def test_a_task_the_step_waits_for_is_answered_only_by_its_instance(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, "sample-case", _case(s["admin"]))
    deadline = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    for number in ("A", "B"):
        await _observe(client, key, "sample.opened", number=number, deadline=deadline)
    await worker.run_once()
    by_key = {i["instanceKey"]: i for i in await _instances(client, key)}
    assert set(by_key) == {"A", "B"}

    await _complete(client, key, _open(by_key["A"], "review")["taskId"], {"decision": "no"})
    await worker.run_once()
    a = await _instance(client, key, by_key["A"]["id"])
    b = await _instance(client, key, by_key["B"]["id"])
    assert a["data"]["decision"] == "no"
    assert [e["id"] for e in a["openElements"]] == ["sign"]
    assert "decision" not in b["data"]
    assert [e["id"] for e in b["openElements"]] == ["review"]


# --- operator commands ------------------------------------------------------------------


async def test_suspend_freezes_timers_resume_restores_them_cancel_closes_the_work(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, "sample-case", _case(s["admin"]))
    deadline = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    await _observe(client, key, "sample.opened", number="S-2", deadline=deadline)
    await worker.run_once()
    [instance] = await _instances(client, key)
    await _complete(client, key, _open(instance, "review")["taskId"], {"decision": "yes"})
    await worker.run_once()
    instance = await _instance(client, key, instance["id"])
    sign_task = _open(instance, "sign")["taskId"]
    path = f"/api/v1/process-instances/{instance['id']}"

    _, reader = await create_agent_with_key(
        client, key, name="reader", permissions=["processes.read"]
    )
    assert (await client.get(path, headers=auth(reader))).status_code == 200
    denied = await client.post(f"{path}:suspend", json={"reason": "hold"}, headers=auth(reader))
    assert denied.status_code == 403, denied.text

    suspended = await client.post(f"{path}:suspend", json={"reason": "hold"}, headers=auth(key))
    assert suspended.status_code == 200, suspended.text
    body = suspended.json()
    assert body["status"] == "suspended"
    assert {t["state"] for t in body["timers"]} == {"frozen"}
    assert all(t["dueAt"] is None for t in body["timers"])
    again = await client.post(f"{path}:suspend", json={"reason": "hold"}, headers=auth(key))
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "invalid_process_instance_state"

    resumed = await client.post(f"{path}:resume", json={}, headers=auth(key))
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "running"
    assert {t["state"] for t in resumed.json()["timers"]} == {"pending"}
    assert all(t["dueAt"] for t in resumed.json()["timers"])

    cancelled = await client.post(
        f"{path}:cancel", json={"reason": "withdrawn", "compensate": False}, headers=auth(key)
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["timers"] == []
    [event] = await _events(client, key, "process.cancelled")
    assert event["payload"]["reason"] == "withdrawn"
    assert event["actorId"] == s["agent"]
    assert (await _task(client, key, sign_task))["systemStatusCategory"] == "terminal_cancelled"
    journal = await _journal(client, key, instance["id"], kind="input")
    assert [e["data"]["input"]["body"]["action"] for e in journal[-3:]] == [
        "suspend",
        "resume",
        "cancel",
    ]
    assert journal[-1]["actorId"] == s["admin"], "the operator stands behind the command"

    closed = await client.post(f"{path}:resume", json={}, headers=auth(key))
    assert closed.status_code == 409
    missing = await client.get(f"/api/v1/process-instances/{uuid.uuid4()}", headers=auth(key))
    assert missing.status_code == 404


# --- explicit start and a standing goal (TAI-ADR-0055) ------------------------------


async def test_an_instance_started_explicitly_is_one_per_key(client: httpx.AsyncClient) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, "sample-goal", GOAL)
    body = {"process": "sample-goal", "key": "goal-1", "data": {"open": 2}}

    _, reader = await create_agent_with_key(
        client, key, name="reader", permissions=["processes.read"]
    )
    denied = await client.post("/api/v1/process-instances", json=body, headers=auth(reader))
    assert denied.status_code == 403, denied.text

    created = await client.post("/api/v1/process-instances", json=body, headers=auth(key))
    assert created.status_code == 201, created.text
    instance = created.json()
    assert (instance["instanceKey"], instance["status"], instance["data"]) == (
        "goal-1",
        "running",
        {"open": 2},
    )
    assert instance["stages"] == [{"id": "watch", "state": "active"}]
    [started] = await _events(client, key, "process.started")
    assert started["payload"]["triggerType"] == "command"
    assert started["payload"]["triggerEventId"] is None

    repeated = await client.post("/api/v1/process-instances", json=body, headers=auth(key))
    assert repeated.status_code == 409, repeated.text
    error = repeated.json()["error"]
    assert error["code"] == "process_instance_exists"
    assert error["details"]["instanceId"] == instance["id"]
    assert len(await _instances(client, key)) == 1

    invalid = await client.post(
        "/api/v1/process-instances",
        json={"process": "sample-goal", "key": "goal-2", "data": {"open": "many"}},
        headers=auth(key),
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_process_data"
    unknown = await client.post(
        "/api/v1/process-instances", json={"process": "nothing", "key": "k"}, headers=auth(key)
    )
    assert unknown.status_code == 404


async def test_a_standing_goal_reaches_loses_and_reaches_its_milestone_again(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, "sample-goal", GOAL)
    created = await client.post(
        "/api/v1/process-instances",
        json={"process": "sample-goal", "key": "goal-1", "data": {"open": 2}},
        headers=auth(key),
    )
    assert created.status_code == 201, created.text

    for open_items in (0, 3, 0):
        await _observe(client, key, "sample.count", goal="goal-1", open=open_items)
        await worker.run_once()

    reached = await _events(client, key, "process.milestone_reached")
    lost = await _events(client, key, "process.milestone_lost")
    assert [e["payload"]["milestone"] for e in reached] == ["clear", "clear"]
    assert [(e["payload"]["milestone"], e["payload"]["stage"]) for e in lost] == [
        ("clear", "watch")
    ]
    assert reached[0]["sequence"] < lost[0]["sequence"] < reached[1]["sequence"]
    instance = await _instance(client, key, created.json()["id"])
    assert instance["status"] == "running"
    milestones = await _journal(client, key, instance["id"], kind="milestone")
    assert [e["data"]["decision"] for e in milestones] == [
        "milestone_reached",
        "milestone_lost",
        "milestone_reached",
    ]


# --- approvals ---------------------------------------------------------------------------


def _vote(approvers: list[str], **approve: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "displayName": "Sample vote",
        "identity": {"agent": AGENT},
        "owner": [{"role": "lead"}],
        "data": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string"},
                "author": {"type": "string"},
            },
        },
        "start": {"on": {"observation": "sample.vote"}, "key": "event.payload.data.number"},
        "stages": [
            {
                "id": "s",
                "steps": [
                    {
                        "id": "vote",
                        "approve": {
                            "approvers": [{"principal": p} for p in approvers],
                            **approve,
                        },
                        "output": {"as": {"verdict": "step.result.outcome"}},
                    },
                    {"id": "done", "complete": {"outcome": "decided"}},
                ],
            }
        ],
    }


async def _deciders(client: httpx.AsyncClient, key: str) -> list[tuple[str, str]]:
    out = []
    for name in ("first", "second"):
        principal, decider_key = await create_agent_with_key(
            client, key, name=name, permissions=["approvals.decide", "approvals.read"]
        )
        out.append((principal["id"], decider_key))
    return out


async def _start(client: httpx.AsyncClient, key: str, process: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/process-instances",
        json={"process": process, "key": "V-1"},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_the_first_approval_of_quorum_any_decides_and_closes_the_rest(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["key"]
    (first, first_key), (second, _) = await _deciders(client, key)
    await _publish(client, key, "sample-vote", _vote([first, second], quorum="any"))
    instance = await _start(client, key, "sample-vote")
    approval_ids = _open(instance, "vote")["approvalIds"]
    assert len(approval_ids) == 2
    approvals = {}
    for approval_id in approval_ids:
        response = await client.get(f"/api/v1/approvals/{approval_id}", headers=auth(key))
        assert response.status_code == 200, response.text
        approvals[response.json()["assignedPrincipalId"]] = response.json()
    assert set(approvals) == {first, second}
    assert all(a["requestedByPrincipalId"] == s["agent"] for a in approvals.values())

    decided = await client.post(
        f"/api/v1/approvals/{approvals[first]['id']}:approve", json={}, headers=auth(first_key)
    )
    assert decided.status_code == 200, decided.text
    await worker.run_once()

    instance = await _instance(client, key, instance["id"])
    assert (instance["status"], instance["outcome"]) == ("completed", "decided")
    assert instance["data"]["verdict"] == "approved"
    other = await client.get(f"/api/v1/approvals/{approvals[second]['id']}", headers=auth(key))
    assert other.json()["status"] == "cancelled"
    votes = await _journal(client, key, instance["id"], kind="vote")
    assert [e["data"]["decision"] for e in votes] == ["vote", "approval_decided"]
    assert votes[0]["actorId"] == first


async def test_a_sequential_step_asks_the_next_approver_after_each_vote(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["key"]
    (first, first_key), (second, second_key) = await _deciders(client, key)
    await _publish(
        client, key, "sample-vote", _vote([first, second], quorum="all", mode="sequential")
    )
    instance = await _start(client, key, "sample-vote")
    [approval_id] = _open(instance, "vote")["approvalIds"]
    response = await client.post(
        f"/api/v1/approvals/{approval_id}:approve", json={}, headers=auth(first_key)
    )
    assert response.status_code == 200, response.text
    await worker.run_once()

    instance = await _instance(client, key, instance["id"])
    assert instance["status"] == "running"
    ids = _open(instance, "vote")["approvalIds"]
    assert len(ids) == 2
    [next_id] = [i for i in ids if i != approval_id]
    response = await client.post(
        f"/api/v1/approvals/{next_id}:approve", json={}, headers=auth(second_key)
    )
    assert response.status_code == 200, response.text
    await worker.run_once()
    instance = await _instance(client, key, instance["id"])
    assert (instance["status"], instance["data"]["verdict"]) == ("completed", "approved")


async def test_separation_of_duties_is_refused_until_the_core_enforces_it(
    client: httpx.AsyncClient,
) -> None:
    """Not dropped silently (CP-ADR-0074 §7): the step fails ``intent_failed``."""
    s = await _setup(client)
    key = s["key"]
    (first, _), (second, _) = await _deciders(client, key)
    spec = _vote([first, second], quorum="any", separationOfDuties=f"['{first}']")
    await _publish(client, key, "sample-vote", spec)
    instance = await _start(client, key, "sample-vote")
    assert instance["status"] == "failed"
    [failed] = await _events(client, key, "process.failed")
    assert failed["payload"]["error"]["type"] == "intent_failed"
    assert failed["payload"]["error"]["detail"] == "not_implemented"
    intents = await _journal(client, key, instance["id"], kind="intent")
    [request] = [e for e in intents if e["data"]["intent"] == "request_approvals"]
    assert request["data"]["executed"]["code"] == "not_implemented"
    assert request["data"]["excludedPrincipals"] == [first]
