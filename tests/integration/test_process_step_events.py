"""Step events of a live instance (CP-ADR-0074 §13, amendment 2026-09-29; P007).

``take()`` projects every engine step onto the core journal: a waiting step
opened is ``process.step_entered``, closed — ``process.step_exited``, written
in the step's transaction after its intents. The acceptance of P007 on the
live core: every waiting step is exactly one pair by ``activityId``; instant
steps give none (SC-010); the same inputs again and a worker restarted
between steps add nothing and lose nothing (SC-002); the events are read with
``events.read`` through ``/events`` and ``/events/ws``, not without it.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from starlette.websockets import WebSocketDisconnect

from control_plane.config import Settings
from control_plane.main import create_app
from control_plane.worker.main import Worker
from tests.helpers import auth, create_agent_with_key
from tests.integration.test_process_instances import (
    AGENT,
    _complete,
    _deciders,
    _instance,
    _open,
    _publish,
    _setup,
    _start,
    _task,
    _vote,
)

ENTERED = "process.step_entered"
EXITED = "process.step_exited"


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


def _flow(admin: str, approver: str) -> dict[str, Any]:
    """Instant steps around three waiting ones: a task, an approval, a pause."""
    return {
        "version": 1,
        "displayName": "Sample flow",
        "identity": {"agent": AGENT},
        "owner": [{"role": "lead"}],
        "data": {
            "type": "object",
            "properties": {
                "note": {"type": "string"},
                "decision": {"type": "string"},
                "verdict": {"type": "string"},
            },
        },
        "start": {"on": {"observation": "sample.flow"}, "key": "event.payload.data.number"},
        "stages": [
            {
                "id": "work",
                "steps": [
                    {"id": "prepare", "set": {"note": "'ready'"}},
                    {
                        "id": "review",
                        "human": {"taskType": "review", "assign": [{"principal": admin}]},
                        "output": {"as": {"decision": "step.result.decision"}},
                    },
                    {"id": "noted", "set": {"note": "'reviewed'"}},
                    {
                        "id": "vote",
                        "approve": {"approvers": [{"principal": approver}], "quorum": "any"},
                        "output": {"as": {"verdict": "step.result.outcome"}},
                    },
                    {"id": "pause", "wait": "PT1S"},
                    {"id": "done", "complete": {"outcome": "ok"}},
                ],
            }
        ],
    }


async def _step_events(
    client: httpx.AsyncClient, key: str, instance_id: str
) -> list[dict[str, Any]]:
    response = await client.get(
        "/api/v1/events", params={"types": "process.step_", "limit": 200}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    return [e for e in response.json()["items"] if e["entityId"] == instance_id]


def _pairs(events: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for event in events:
        out.setdefault(event["payload"]["activityId"], []).append(event["type"])
    return out


def _redeliver(sync_engine: Engine) -> None:
    """The worker's cursor back to the start: every input comes again."""
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE event_consumer_cursors SET tx_id = 0, sequence = 0 WHERE name = :n"),
            {"n": "processes"},
        )


async def _run(settings: Settings) -> None:
    """One pass of a freshly started worker: a restart between steps."""
    restarted = Worker(settings)
    try:
        await restarted.run_once()
    finally:
        await restarted.engine.dispose()


async def _flow_to_the_end(
    client: httpx.AsyncClient, settings: Settings, worker: Worker, sync_engine: Engine
) -> dict[str, Any]:
    s = await _setup(client)
    key = s["key"]
    (approver, approver_key), _ = await _deciders(client, key)
    await _publish(client, key, "sample-flow", _flow(s["admin"], approver))
    started = await client.post(
        "/api/v1/process-instances",
        json={"process": "sample-flow", "key": "F-1"},
        headers=auth(key),
    )
    assert started.status_code == 201, started.text
    instance = started.json()
    review = _open(instance, "review")

    await _complete(client, key, review["taskId"], {"decision": "yes"})
    await worker.run_once()
    _redeliver(sync_engine)
    await _run(settings)
    instance = await _instance(client, key, instance["id"])
    vote = _open(instance, "vote")

    decided = await client.post(
        f"/api/v1/approvals/{vote['approvalIds'][0]}:approve", json={}, headers=auth(approver_key)
    )
    assert decided.status_code == 200, decided.text
    await _run(settings)
    _redeliver(sync_engine)
    await worker.run_once()

    await asyncio.sleep(1.2)  # the pause's timer falls due
    await _run(settings)
    await worker.run_once()
    instance = await _instance(client, key, instance["id"])
    assert (instance["status"], instance["outcome"]) == ("completed", "ok")
    return {**s, "instance": instance, "review": review, "vote": vote}


async def test_every_waiting_step_is_one_pair_and_instant_steps_give_none(
    client: httpx.AsyncClient, settings: Settings, worker: Worker, sync_engine: Engine
) -> None:
    flow = await _flow_to_the_end(client, settings, worker, sync_engine)
    key, instance = flow["key"], flow["instance"]
    events = await _step_events(client, key, instance["id"])

    # One pair per activity, three waiting steps, six events; set/complete give none.
    assert list(_pairs(events).values()) == [[ENTERED, EXITED]] * 3
    assert [e["payload"]["element"] for e in events if e["type"] == ENTERED] == [
        "review",
        "vote",
        "pause",
    ]
    assert len(events) == 2 * 3

    by = {(e["type"], e["payload"]["element"]): e for e in events}
    review = by[(ENTERED, "review")]["payload"]
    assert (review["stepKind"], review["waitsFor"], review["stage"]) == ("human", "task", "work")
    assert review["taskId"] == flow["review"]["taskId"]
    assert (review["attempt"], review["approvalIds"], review["due"]) == (1, [], None)
    assert review["workspaceId"] is None
    assert (review["instanceKey"], review["definitionKey"], review["version"]) == (
        "F-1",
        "sample-flow",
        1,
    )
    vote = by[(ENTERED, "vote")]["payload"]
    assert (vote["stepKind"], vote["waitsFor"]) == ("approve", "approval")
    assert vote["approvalIds"] == flow["vote"]["approvalIds"]
    pause = by[(ENTERED, "pause")]["payload"]
    assert (pause["stepKind"], pause["waitsFor"]) == ("wait", "time")
    for element in ("review", "vote", "pause"):
        exited = by[(EXITED, element)]
        assert exited["payload"]["outcome"] == "completed"
        assert exited["payload"]["attempt"] == 1
        assert exited["payload"]["enteredAt"] == by[(ENTERED, element)]["payload"]["enteredAt"]
        assert exited["payload"]["durationSeconds"] >= 0
        assert (exited["payload"]["breached"], exited["payload"]["overdueSeconds"]) == (
            False,
            None,
        )
        # The process acts as its identity agent.
        assert exited["actorId"] == flow["agent"]
    assert by[(EXITED, "pause")]["payload"]["durationSeconds"] >= 1

    with sync_engine.connect() as conn:
        attempts, refs = conn.execute(
            text("SELECT step_attempts, refs FROM process_instances WHERE id = :id"),
            {"id": instance["id"]},
        ).one()
    assert attempts == {"review": 1, "vote": 1, "pause": 1}
    # The attempt an activity entered with is kept only while it is open.
    assert not [ref for ref, target in refs.items() if "attempt" in target], refs

    # The engine's journal is untouched: no step decision, no emit_event of a step.
    response = await client.get(
        f"/api/v1/process-instances/{instance['id']}/journal",
        params={"limit": 200},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    assert "step_entered" not in response.text and "step_exited" not in response.text


async def test_redelivered_inputs_and_a_restarted_worker_write_no_step_event_twice(
    client: httpx.AsyncClient, settings: Settings, worker: Worker, sync_engine: Engine
) -> None:
    flow = await _flow_to_the_end(client, settings, worker, sync_engine)
    key, instance = flow["key"], flow["instance"]
    before = await _step_events(client, key, instance["id"])

    _redeliver(sync_engine)
    await _run(settings)
    await worker.run_once()

    after = await _step_events(client, key, instance["id"])
    assert [e["id"] for e in after] == [e["id"] for e in before]
    assert list(_pairs(after).values()) == [[ENTERED, EXITED]] * 3


async def test_a_cancelled_instance_exits_its_open_step_cancelled(
    client: httpx.AsyncClient, settings: Settings, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["key"]
    (approver, _), _ = await _deciders(client, key)
    await _publish(client, key, "sample-flow", _flow(s["admin"], approver))
    started = await client.post(
        "/api/v1/process-instances",
        json={"process": "sample-flow", "key": "F-2"},
        headers=auth(key),
    )
    assert started.status_code == 201, started.text
    instance_id = started.json()["id"]
    cancelled = await client.post(
        f"/api/v1/process-instances/{instance_id}:cancel",
        json={"reason": "withdrawn", "compensate": False},
        headers=auth(key),
    )
    assert cancelled.status_code == 200, cancelled.text
    events = await _step_events(client, key, instance_id)
    assert [(e["type"], e["payload"]["element"]) for e in events] == [
        (ENTERED, "review"),
        (EXITED, "review"),
    ]
    assert events[1]["payload"]["outcome"] == "cancelled"


async def _replayed(client: httpx.AsyncClient, key: str, process: str, spec: dict[str, Any]):
    """The instances' journals replayed on their own version: nothing diverges."""
    response = await client.post(
        f"/api/v1/process-definitions/{process}:replay", json={"spec": spec}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["replayed"], body["diverged"]) == (1, 0), body


async def test_a_task_cancelled_by_a_participant_exits_withdrawn_as_that_participant(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["key"]
    (approver, _), _ = await _deciders(client, key)
    spec = _flow(s["admin"], approver)
    await _publish(client, key, "sample-flow", spec)
    participant, participant_key = await create_agent_with_key(
        client, key, name="participant", permissions=["tasks.read", "tasks.write"]
    )
    started = await client.post(
        "/api/v1/process-instances",
        json={"process": "sample-flow", "key": "F-4"},
        headers=auth(key),
    )
    assert started.status_code == 201, started.text
    instance_id = started.json()["id"]
    task = await _task(client, key, _open(started.json(), "review")["taskId"])
    cancelled = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "cancelled"},
        headers={**auth(participant_key), "If-Match": f'"task-{task["version"]}"'},
    )
    assert cancelled.status_code == 200, cancelled.text
    await worker.run_once()

    entered, exited = await _step_events(client, key, instance_id)
    assert (entered["type"], exited["type"]) == (ENTERED, EXITED)
    assert exited["payload"]["outcome"] == "withdrawn"
    # Who withdrew the step is the event's actor; the entry stays the process's.
    assert exited["actorId"] == participant["id"]
    assert entered["actorId"] == s["agent"]
    await _replayed(client, key, "sample-flow", spec)


async def _cancel(client: httpx.AsyncClient, key: str, approval_id: str) -> None:
    response = await client.post(
        f"/api/v1/approvals/{approval_id}:cancel", json={}, headers=auth(key)
    )
    assert response.status_code == 200, response.text


async def _votes(
    client: httpx.AsyncClient, key: str, instance: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """The step's approvals by their approver."""
    out = {}
    for approval_id in _open(instance, "vote")["approvalIds"]:
        response = await client.get(f"/api/v1/approvals/{approval_id}", headers=auth(key))
        assert response.status_code == 200, response.text
        out[response.json()["assignedPrincipalId"]] = response.json()
    return out


@pytest.mark.parametrize(
    ("quorum", "first_votes", "verdict", "outcome"),
    [
        # quorum all: the first approved, the second withdrawn — the rule decided it.
        ("all", "approve", "approved", "completed"),
        # atLeast 2 is out of reach once one approver left: a rejection by quorum.
        ({"atLeast": 2}, None, "rejected", "completed"),
        # Everybody withdrawn: nobody is left to vote (no_approvers).
        ("all", "cancel", "rejected", "withdrawn"),
    ],
)
async def test_an_approve_step_exits_as_its_voting_rule_decided(
    client: httpx.AsyncClient,
    worker: Worker,
    quorum: Any,
    first_votes: str | None,
    verdict: str,
    outcome: str,
) -> None:
    s = await _setup(client)
    key = s["key"]
    (first, first_key), (second, _) = await _deciders(client, key)
    manager, manager_key = await create_agent_with_key(
        client, key, name="manager", permissions=["approvals.manage", "approvals.read"]
    )
    spec = _vote([first, second], quorum=quorum)
    await _publish(client, key, "sample-vote", spec)
    instance = await _start(client, key, "sample-vote")
    votes = await _votes(client, key, instance)
    if first_votes == "approve":
        response = await client.post(
            f"/api/v1/approvals/{votes[first]['id']}:approve", json={}, headers=auth(first_key)
        )
        assert response.status_code == 200, response.text
        await worker.run_once()
    elif first_votes == "cancel":
        await _cancel(client, manager_key, votes[first]["id"])
        await worker.run_once()
        # One approver left: the step waits for them, no exit yet.
        assert [e["type"] for e in await _step_events(client, key, instance["id"])] == [ENTERED]
    await _cancel(client, manager_key, votes[second]["id"])
    await worker.run_once()

    instance = await _instance(client, key, instance["id"])
    assert (instance["status"], instance["data"]["verdict"]) == ("completed", verdict)
    entered, exited = await _step_events(client, key, instance["id"])
    assert (entered["type"], exited["type"]) == (ENTERED, EXITED)
    assert exited["payload"]["outcome"] == outcome
    assert exited["actorId"] == (manager["id"] if outcome == "withdrawn" else s["agent"])
    await _replayed(client, key, "sample-vote", spec)


async def test_step_events_are_read_with_events_read_only(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    s = await _setup(client)
    key = s["key"]
    (approver, _), _ = await _deciders(client, key)
    await _publish(client, key, "sample-flow", _flow(s["admin"], approver))
    started = await client.post(
        "/api/v1/process-instances",
        json={"process": "sample-flow", "key": "F-3"},
        headers=auth(key),
    )
    assert started.status_code == 201, started.text
    instance_id = started.json()["id"]

    _, reader = await create_agent_with_key(client, key, name="reader", permissions=["events.read"])
    _, outsider = await create_agent_with_key(
        client, key, name="outsider", permissions=["processes.read"]
    )
    [entered] = await _step_events(client, reader, instance_id)
    assert (entered["type"], entered["payload"]["element"]) == (ENTERED, "review")
    denied = await client.get(
        "/api/v1/events", params={"types": "process.step_"}, headers=auth(outsider)
    )
    assert denied.status_code == 403, denied.text

    with TestClient(create_app(settings)) as tc:
        with tc.websocket_connect(
            "/api/v1/events/ws?types=process.step_", headers=auth(reader)
        ) as ws:
            received = ws.receive_json()
        assert (received["type"], received["entityId"]) == (ENTERED, instance_id)
        assert received["payload"]["activityId"] == entered["payload"]["activityId"]
        with (
            tc.websocket_connect(
                "/api/v1/events/ws?types=process.step_", headers=auth(outsider)
            ) as ws,
            pytest.raises(WebSocketDisconnect) as refused,
        ):
            ws.receive_json()
        assert refused.value.code == 4403
