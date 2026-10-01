"""The basis of an ``external_write`` a process step calls (CP-ADR-0056 §4, amendment TASK-001197).

A process step ``call: {skill}`` invokes the skill as the identity of the
process, with no task, run or gate approval behind it. An ``external_write``
skill needs a basis: the instance of the process is it — its published
version names this very skill version, as a task type names its
``execution``. The basis holds while the step's activity is open: a
cancelled instance, a timed out or retried step, a failed instance cancel
the call at the claim (``basis_revoked``), a suspended instance holds it. No
route can cite the basis: the same identity calling the skill directly still
gets ``403``.
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
from control_plane.worker.main import Worker
from tests.helpers import auth, create_agent_with_key, do_bootstrap

AGENT = "sample-notifier"
PROCESS = "sample-notify"
SKILL = "sample.notify"
ENTRYPOINT = "tests.stubs:notify"
ISSUER = "https://iam.example.test"
AGENT_PERMISSIONS = ["events.read", "skills.invoke", "tasks.read", "tasks.write"]
EXECUTOR_PERMISSIONS = ["skills.execute", "sessions.open"]


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


def _process(notify: dict[str, Any] | None = None) -> dict[str, Any]:
    """Notify on each opened case, then wait for the answer and close."""
    call = {"id": "notify", "call": {"skill": f"{SKILL}@1", "input": {"text": "data.number"}}}
    return {
        "version": 1,
        "displayName": "Sample notify",
        "identity": {"agent": AGENT},
        "data": {"type": "object", "properties": {"number": {"type": "string"}}},
        "start": {
            "on": {"observation": "sample.opened"},
            "key": "event.payload.data.number",
            "set": {"number": "string(event.payload.data.number)"},
        },
        "stages": [
            {
                "id": "work",
                "steps": [
                    notify or call,
                    {"id": "done", "complete": {"outcome": "notified"}},
                ],
            }
        ],
    }


async def _setup(
    client: httpx.AsyncClient,
    *,
    side_effects: str = "external_write",
    notify: dict[str, Any] | None = None,
) -> dict[str, Any]:
    boot = await do_bootstrap(client)
    key: str = boot["apiKey"]["key"]
    responses = [
        await client.post(
            "/api/v1/skills",
            json={
                "name": SKILL,
                "version": "1",
                "sideEffects": side_effects,
                "riskLevel": "medium",
                "contract": {
                    "inputs": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "outputs": {"type": "object"},
                    "idempotency": "required",
                    "implementation": {"protocol": "local", "entrypoint": ENTRYPOINT},
                },
            },
            headers=auth(key),
        ),
        await client.post(
            "/api/v1/agents",
            json={
                "key": AGENT,
                "spec": {
                    "displayName": "Sample notifier",
                    "identity": {"kind": "service", "permissions": AGENT_PERMISSIONS},
                    "placement": "none",
                },
            },
            headers=auth(key),
        ),
    ]
    for response in responses:
        assert response.status_code in (200, 201), response.text
    _, fleet_key = await create_agent_with_key(
        client,
        key,
        name=f"fleet-{uuid.uuid4().hex[:6]}",
        kind="service",
        permissions=["agents.read", "agents.status.write"],
    )
    linked = await client.put(
        f"/api/v1/agents/{AGENT}/identity",
        json={
            "issuer": ISSUER,
            "iamTenantId": str(uuid.uuid4()),
            "iamPrincipalId": str(uuid.uuid4()),
        },
        headers=auth(fleet_key),
    )
    assert linked.status_code == 200, linked.text
    published = await client.post(
        "/api/v1/process-definitions",
        json={"key": PROCESS, "spec": _process(notify)},
        headers=auth(key),
    )
    assert published.status_code == 201, published.text
    _, executor_key = await create_agent_with_key(
        client, key, name="executor", permissions=EXECUTOR_PERMISSIONS
    )
    return {"key": key, "executor": executor_key, "agent": linked.json()["principalId"]}


async def _start(client: httpx.AsyncClient, worker: Worker, key: str, number: str) -> str:
    response = await client.post(
        "/api/v1/observations",
        json={"kind": "sample.opened", "content": "opened", "data": {"number": number}},
        headers=auth(key),
    )
    assert response.status_code in (200, 201), response.text
    await worker.run_once()
    found = await client.get(
        "/api/v1/process-instances",
        params={"definitionKey": PROCESS},
        headers=auth(key),
    )
    assert found.status_code == 200, found.text
    [instance] = [i for i in found.json()["items"] if i["instanceKey"] == number]
    instance_id: str = instance["id"]
    return instance_id


def _calls(sync_engine: Engine) -> list[Any]:
    with sync_engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT id, status, authorization_basis, error, authority_principal_id"
                    " FROM skill_invocations ORDER BY created_at"
                )
            ).all()
        )


async def _journal(client: httpx.AsyncClient, key: str, instance_id: str) -> list[dict[str, Any]]:
    response = await client.get(
        f"/api/v1/process-instances/{instance_id}/journal",
        params={"limit": 50},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def _claim(client: httpx.AsyncClient, key: str) -> httpx.Response:
    return await client.post(
        "/api/v1/skill-invocations:claim",
        json={"protocols": ["local"], "localEntrypoints": [ENTRYPOINT]},
        headers=auth(key),
    )


async def _command(client: httpx.AsyncClient, key: str, instance_id: str, action: str) -> None:
    response = await client.post(
        f"/api/v1/process-instances/{instance_id}:{action}",
        json={"reason": f"{action} for the test"},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text


async def test_a_process_step_calls_an_external_write_skill_on_the_basis_of_its_instance(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    instance_id = await _start(client, worker, s["key"], "N-1")

    journal = await _journal(client, s["key"], instance_id)
    [asked] = [e for e in journal if e["kind"] == "intent" and e["reason"] == "invoke_skill"]
    assert asked["data"]["executed"]["ok"] is True, asked["data"]["executed"]
    [call] = _calls(sync_engine)
    assert call.status == "pending"
    assert call.authorization_basis == {
        "kind": "process",
        "instanceId": instance_id,
        "activityId": asked["data"]["activityId"],
        "definitionKey": PROCESS,
        "definitionVersion": 1,
    }
    assert str(call.authority_principal_id) == s["agent"]

    claimed = await _claim(client, s["executor"])
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["invocation"]["id"] == str(call.id)


async def test_the_basis_cannot_be_cited_by_a_route(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    """The process's identity calling the skill itself has no basis: only the engine gives one."""
    s = await _setup(client)
    issued = await client.post(
        f"/api/v1/principals/{s['agent']}/api-keys",
        json={"permissions": ["skills.invoke"]},
        headers=auth(s["key"]),
    )
    assert issued.status_code == 201, issued.text
    caller_key = issued.json()["key"]
    response = await client.post(
        f"/api/v1/skills/{SKILL}@1:invoke",
        json={"inputs": {"text": "x"}, "idempotencyKey": "direct-1"},
        headers=auth(caller_key),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "skill_side_effect_not_authorized"


async def test_a_cancelled_instance_takes_the_basis_of_its_pending_call_away(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    instance_id = await _start(client, worker, s["key"], "N-2")
    await _command(client, s["key"], instance_id, "cancel")

    assert (await _claim(client, s["executor"])).status_code == 204
    [call] = _calls(sync_engine)
    assert call.status == "cancelled"
    assert (call.error["code"], call.error["message"]) == ("basis_revoked", "process_cancelled")


async def test_a_suspended_instance_holds_its_call_until_resumed(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    instance_id = await _start(client, worker, s["key"], "N-3")
    await _command(client, s["key"], instance_id, "suspend")

    assert (await _claim(client, s["executor"])).status_code == 204
    [call] = _calls(sync_engine)
    assert call.status == "pending"

    await _command(client, s["key"], instance_id, "resume")
    claimed = await _claim(client, s["executor"])
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["invocation"]["id"] == str(call.id)


async def test_a_skill_without_external_writes_keeps_no_basis(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """Only an ``external_write`` records the instance: other calls stay as they were."""
    s = await _setup(client, side_effects="none")
    instance_id = await _start(client, worker, s["key"], "N-4")
    await _command(client, s["key"], instance_id, "cancel")

    [call] = _calls(sync_engine)
    assert call.authorization_basis is None
    claimed = await _claim(client, s["executor"])
    assert claimed.status_code == 200, claimed.text


def _guarded(**around: Any) -> dict[str, Any]:
    """The notify call in a ``try`` with a one-hour timeout."""
    call = {"skill": f"{SKILL}@1", "input": {"text": "data.number"}, "timeout": "PT1H"}
    return {"id": "guard", "try": {"do": [{"id": "notify", "call": call}], **around}}


async def _time_out(worker: Worker, sync_engine: Engine) -> None:
    """The timeout of the notify call comes due, and the worker fires it."""
    with sync_engine.begin() as conn:
        moved = conn.execute(
            text("UPDATE process_timers SET due_at = :at WHERE element = 'notify'"),
            {"at": datetime.now(UTC) - timedelta(seconds=1)},
        )
        assert moved.rowcount == 1
    await worker.run_once()


async def _status(client: httpx.AsyncClient, key: str, instance_id: str) -> str:
    response = await client.get(f"/api/v1/process-instances/{instance_id}", headers=auth(key))
    assert response.status_code == 200, response.text
    status: str = response.json()["status"]
    return status


async def test_a_retried_step_hands_out_one_call_and_cancels_the_timed_out_one(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """Review of TASK-001197: a timeout and a retry left two pending calls, both claimable."""
    s = await _setup(client, notify=_guarded(retry={"limit": 1}))
    instance_id = await _start(client, worker, s["key"], "N-5")
    await _time_out(worker, sync_engine)

    first, second = _calls(sync_engine)
    claimed = await _claim(client, s["executor"])
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["invocation"]["id"] == str(second.id)
    assert (await _claim(client, s["executor"])).status_code == 204

    first, second = _calls(sync_engine)
    assert first.authorization_basis["activityId"] != second.authorization_basis["activityId"]
    assert first.status == "cancelled"
    assert (first.error["code"], first.error["message"]) == ("basis_revoked", "process_step_closed")
    assert second.status == "running"
    assert await _status(client, s["key"], instance_id) == "running"


async def test_a_timeout_caught_by_the_step_cancels_the_call(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    catch = [{"errors": {"type": "timeout"}, "do": [{"id": "gave-up", "set": {"number": "'-'"}}]}]
    s = await _setup(client, notify=_guarded(catch=catch))
    instance_id = await _start(client, worker, s["key"], "N-6")
    await _time_out(worker, sync_engine)
    assert await _status(client, s["key"], instance_id) == "completed"

    assert (await _claim(client, s["executor"])).status_code == 204
    [call] = _calls(sync_engine)
    assert call.status == "cancelled"
    assert (call.error["code"], call.error["message"]) == ("basis_revoked", "process_step_closed")


async def test_a_failed_instance_takes_the_basis_of_its_pending_call_away(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """A timeout no handler takes fails the instance: its step is closed, so is the basis."""
    call = {"skill": f"{SKILL}@1", "input": {"text": "data.number"}, "timeout": "PT1H"}
    s = await _setup(client, notify={"id": "notify", "call": call})
    instance_id = await _start(client, worker, s["key"], "N-7")
    await _time_out(worker, sync_engine)
    assert await _status(client, s["key"], instance_id) == "failed"

    assert (await _claim(client, s["executor"])).status_code == 204
    [pending] = _calls(sync_engine)
    assert pending.status == "cancelled"
    assert (pending.error["code"], pending.error["message"]) == (
        "basis_revoked",
        "process_step_closed",
    )
