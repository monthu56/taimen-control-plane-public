"""M1.6 verification stage: acceptance checks run before a task is done (CP-ADR-0067).

A task with acceptance checks handed in along any of the three completion
paths — ``:complete``, a run's ``:succeed``, an approval outcome's
``completeTask`` — is not done until the worker has run its checks; a
``deterministic`` check is a skill call made with the completer's authority.
A failed attempt returns the task to its executor, the third failure in a row
to a person. A ``human`` or ``llm_judge`` check is a person's decision on a
gate approval of the task. Skills and events are named neutrally: the stage knows nothing
of what is checked.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.worker.main import Worker
from tests.helpers import (
    auth,
    claim_task,
    create_agent_with_key,
    create_role,
    create_task,
    do_bootstrap,
    open_session,
)

ENTRYPOINT = "sample.check:run"
SKILL = "check.sample@1"
CHECK = {
    "key": "sample-holds",
    "kind": "deterministic",
    "description": "The sample check holds for this task",
    "spec": {
        "skill": SKILL,
        "inputs": {"subject": "$.task.publicId"},
        "expect": {"ok": True},
    },
}
RUNNER_PERMISSIONS = [
    "sessions.open",
    "tasks.read",
    "tasks.write",
    "tasks.claim",
    "events.read",
    "skills.invoke",
]


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


async def _publish(
    client: httpx.AsyncClient, key: str, name: str = "check.sample", side_effects: str = "none"
) -> None:
    response = await client.post(
        "/api/v1/skills",
        json={
            "name": name,
            "version": "1",
            "sideEffects": side_effects,
            "riskLevel": "low",
            "contract": {
                "inputs": {
                    "type": "object",
                    "properties": {"subject": {"type": "string", "minLength": 1}},
                    "required": ["subject"],
                },
                "outputs": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
                "implementation": {"protocol": "local", "entrypoint": ENTRYPOINT},
            },
        },
        headers=auth(key),
    )
    assert response.status_code == 201, response.text


async def _setup(client: httpx.AsyncClient) -> dict[str, Any]:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    await _publish(client, admin_key)
    _, executor_key = await create_agent_with_key(
        client, admin_key, name="executor", permissions=["skills.execute"]
    )
    return {
        "admin_key": admin_key,
        "admin_id": boot["adminPrincipal"]["id"],
        "executor_key": executor_key,
    }


async def _task(client: httpx.AsyncClient, key: str, ref: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{ref}", headers=auth(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _complete(client: httpx.AsyncClient, key: str, ref: str) -> httpx.Response:
    current = await _task(client, key, ref)
    return await client.post(
        f"/api/v1/tasks/{ref}:complete",
        headers={**auth(key), "If-Match": f'"task-{current["version"]}"'},
    )


async def _patch(client: httpx.AsyncClient, key: str, ref: str, **body: Any) -> dict[str, Any]:
    current = await _task(client, key, ref)
    response = await client.patch(
        f"/api/v1/tasks/{ref}",
        json=body,
        headers={**auth(key), "If-Match": f'"task-{current["version"]}"'},
    )
    assert response.status_code == 200, response.text
    patched: dict[str, Any] = response.json()
    return patched


async def _answer(client: httpx.AsyncClient, executor_key: str, output: dict[str, Any]) -> str:
    """The executor takes the queued check call and reports ``output``."""
    claimed = await client.post(
        "/api/v1/skill-invocations:claim",
        json={"protocols": ["local"], "localEntrypoints": [ENTRYPOINT]},
        headers=auth(executor_key),
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["invocation"]
    assert lease is not None
    done = await client.post(
        f"/api/v1/skill-invocations/{lease['id']}:complete",
        json={"fencingToken": lease["fencingToken"], "output": output},
        headers=auth(executor_key),
    )
    assert done.status_code == 200, done.text
    invocation_id: str = lease["id"]
    return invocation_id


async def _verifications(client: httpx.AsyncClient, key: str, ref: str) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/tasks/{ref}/verifications", headers=auth(key))
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def _make_due(sync_engine: Engine) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE task_verifications SET next_check_at = now() "
                "WHERE next_check_at IS NOT NULL"
            )
        )


def _events(sync_engine: Engine, event_type: str, task_id: str) -> list[dict[str, Any]]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT payload, correlation_id FROM events WHERE event_type = :type "
                "AND entity_id = :task ORDER BY sequence"
            ),
            {"type": event_type, "task": task_id},
        ).all()
    return [{**row.payload, "_correlation": row.correlation_id} for row in rows]


async def _verify(
    client: httpx.AsyncClient,
    worker: Worker,
    sync_engine: Engine,
    executor_key: str,
    output: dict[str, Any],
) -> str:
    """One pass queues the check's call, the executor answers, one pass reads it."""
    await worker.run_once()
    invocation_id = await _answer(client, executor_key, output)
    _make_due(sync_engine)
    await worker.run_once()
    return invocation_id


# --- :complete ----------------------------------------------------------------


async def test_complete_opens_an_attempt_and_passing_checks_complete_the_task(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["admin_key"]
    task = await create_task(client, key, title="Handed in", acceptance=[CHECK])

    handed_in = await _complete(client, key, task["id"])
    assert handed_in.status_code == 200, handed_in.text
    body = handed_in.json()
    # Not done: the checks have not run.
    assert body["systemStatusCategory"] != "terminal_success"
    assert body["completedAt"] is None
    assert body["verification"]["status"] == "running"
    assert body["verification"]["attempt"] == 1
    assert _events(sync_engine, "task.completed", task["id"]) == []

    # Handed-in work is not work to take.
    claimability = (
        await client.get(f"/api/v1/tasks/{task['id']}/claimability", headers=auth(key))
    ).json()
    assert claimability["claimable"] is False
    pending = [r for r in claimability["reasons"] if r["code"] == "verification_pending"]
    assert pending == [
        {
            "code": "verification_pending",
            "verificationId": body["verification"]["id"],
            "status": "running",
        }
    ]
    _, agent_key = await create_agent_with_key(client, key, name="runner")
    work_session = await open_session(client, agent_key)
    refused = await claim_task(client, agent_key, task["id"], work_session["id"])
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "verification_pending"
    work = (await client.get("/api/v1/work/available", headers=auth(agent_key))).json()["items"]
    assert task["id"] not in [item["id"] for item in work]

    # Completing again while the attempt runs opens no second one (FR-011).
    again = await _complete(client, key, task["id"])
    assert again.status_code == 200, again.text
    assert again.json()["verification"]["id"] == body["verification"]["id"]
    assert len(_events(sync_engine, "task.verification_started", task["id"])) == 1

    await worker.run_once()
    with sync_engine.connect() as conn:
        call = conn.execute(
            text(
                "SELECT id, requested_by_kind, requested_by_ref, inputs, task_id, "
                "authority_principal_id FROM skill_invocations"
            )
        ).one()
    assert call.requested_by_kind == "verification"
    assert call.requested_by_ref == body["verification"]["id"]
    assert call.inputs == {"subject": task["publicId"]}
    assert str(call.task_id) == task["id"]

    assert await _answer(client, s["executor_key"], {"ok": True}) == str(call.id)
    _make_due(sync_engine)
    await worker.run_once()

    done = await _task(client, key, task["id"])
    assert done["status"] == "done"
    assert done["systemStatusCategory"] == "terminal_success"
    assert done["verification"]["status"] == "passed"
    completed = _events(sync_engine, "task.completed", task["id"])
    verified = _events(sync_engine, "task.verified", task["id"])
    assert len(completed) == len(verified) == 1
    assert completed[0]["verificationId"] == body["verification"]["id"]
    assert verified[0]["results"][0]["key"] == CHECK["key"]
    assert verified[0]["results"][0]["status"] == "passed"
    assert {"kind": "skill_invocation", "ref": str(call.id)} in verified[0]["results"][0][
        "evidence"
    ]
    # Every event of the attempt carries the correlation of the completion.
    started = _events(sync_engine, "task.verification_started", task["id"])[0]
    assert verified[0]["_correlation"] == started["_correlation"]

    artifacts = (
        await client.get(
            f"/api/v1/artifacts?taskId={task['id']}&type=verification", headers=auth(key)
        )
    ).json()["items"]
    assert [(a["type"], a["createdByPrincipalId"]) for a in artifacts] == [
        ("verification", s["admin_id"])
    ]

    attempts = await _verifications(client, key, task["id"])
    assert [(a["attempt"], a["status"], a["trigger"]) for a in attempts] == [
        (1, "passed", "complete")
    ]
    assert attempts[0]["results"][0]["reason"] is None
    assert "authority" not in attempts[0]

    # Nothing more happens: the attempt is closed.
    _make_due(sync_engine)
    await worker.run_once()
    assert len(_events(sync_engine, "task.verified", task["id"])) == 1


async def test_a_task_without_acceptance_completes_as_before(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    task = await create_task(client, s["admin_key"], title="No checks")
    response = await _complete(client, s["admin_key"], task["id"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "done"
    assert response.json()["verification"] is None
    assert len(_events(sync_engine, "task.completed", task["id"])) == 1
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM task_verifications")).scalar() == 0


# --- run :succeed ---------------------------------------------------------------


async def test_run_succeed_hands_the_task_in_instead_of_completing_it(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["admin_key"]
    runner, runner_key = await create_agent_with_key(
        client, key, name="runner", permissions=RUNNER_PERMISSIONS
    )
    task = await create_task(client, key, title="Run it", acceptance=[CHECK])
    work_session = await open_session(client, runner_key)
    claim = (await claim_task(client, runner_key, task["id"], work_session["id"])).json()
    run = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:start-run",
            json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
            headers=auth(runner_key),
        )
    ).json()

    succeeded = await client.post(
        f"/api/v1/runs/{run['id']}:succeed", json={}, headers=auth(runner_key)
    )
    assert succeeded.status_code == 200, succeeded.text
    assert succeeded.json()["run"]["status"] == "succeeded"
    handed_in = succeeded.json()["task"]
    assert handed_in["systemStatusCategory"] != "terminal_success"
    # The claim is released; the status stays where the work left it.
    assert handed_in["activeClaimId"] is None
    assert handed_in["status"] == "in_progress"
    attempts = await _verifications(client, key, task["id"])
    assert [(a["trigger"], a["triggerRef"], a["authorityPrincipalId"]) for a in attempts] == [
        ("run", run["id"], runner["id"])
    ]

    await _verify(client, worker, sync_engine, s["executor_key"], {"ok": True})
    with sync_engine.connect() as conn:
        authority = conn.execute(
            text("SELECT authority_principal_id FROM skill_invocations")
        ).scalar()
    # The check runs with the authority of whoever completed the task.
    assert str(authority) == runner["id"]
    assert (await _task(client, key, task["id"]))["status"] == "done"


# --- approval outcome completeTask -----------------------------------------------


async def test_an_approval_that_completes_the_task_goes_through_the_stage(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["admin_key"]
    created = await client.post(
        "/api/v1/task-types",
        json={
            "key": "sample-gated",
            "displayName": "sample-gated",
            "approvalSchema": {
                "gates": {"default": {"outcomes": {"approved": [{"completeTask": {}}]}}}
            },
        },
        headers=auth(key),
    )
    assert created.status_code == 201, created.text
    task = await create_task(client, key, title="Gated", typeKey="sample-gated", acceptance=[CHECK])
    approval = await client.post(
        "/api/v1/approvals",
        json={"task": task["id"], "assignedPrincipalId": s["admin_id"], "gate": True},
        headers=auth(key),
    )
    assert approval.status_code == 201, approval.text
    decided = await client.post(
        f"/api/v1/approvals/{approval.json()['id']}:approve", json={}, headers=auth(key)
    )
    assert decided.status_code == 200, decided.text

    await worker.run_once()
    outcome = (
        await client.get(f"/api/v1/approvals/{approval.json()['id']}/outcome", headers=auth(key))
    ).json()
    assert outcome["outcomeStatus"] == "executed"
    # The outcome closed nothing by itself: the task is under verification.
    assert (await _task(client, key, task["id"]))["systemStatusCategory"] != "terminal_success"
    attempts = await _verifications(client, key, task["id"])
    assert [(a["trigger"], a["triggerRef"]) for a in attempts] == [
        ("approval", approval.json()["id"])
    ]

    await _verify(client, worker, sync_engine, s["executor_key"], {"ok": True})
    assert (await _task(client, key, task["id"]))["status"] == "done"
    assert len(_events(sync_engine, "task.verified", task["id"])) == 1


# --- failure --------------------------------------------------------------------


async def test_a_failed_attempt_returns_the_task_and_the_third_blocks_it(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["admin_key"]
    task = await create_task(client, key, title="Keeps failing", acceptance=[CHECK])

    for attempt in (1, 2, 3):
        await _patch(client, key, task["id"], status="in_progress")
        assert (await _complete(client, key, task["id"])).status_code == 200
        await _verify(client, worker, sync_engine, s["executor_key"], {"ok": False})

        current = await _task(client, key, task["id"])
        failed = _events(sync_engine, "task.verification_failed", task["id"])[-1]
        assert failed["attempt"] == attempt
        assert failed["failedCheck"] == CHECK["key"]
        assert failed["reason"] == "expectation_not_met"
        assert failed["consecutiveFailures"] == attempt
        assert current["verification"] == {
            **current["verification"],
            "status": "failed",
            "attempt": attempt,
        }
        if attempt < 3:
            # Back to the executor, claimable again, without a person.
            assert failed["blocked"] is False
            assert current["status"] == "todo"
            claimability = (
                await client.get(f"/api/v1/tasks/{task['id']}/claimability", headers=auth(key))
            ).json()
            assert claimability["claimable"] is True, claimability
        else:
            assert failed["blocked"] is True
            assert current["status"] == "blocked"
            assert current["systemStatusCategory"] == "blocked"

    comments = (await client.get(f"/api/v1/tasks/{task['id']}/comments", headers=auth(key))).json()[
        "items"
    ]
    assert len(comments) == 3
    assert "sample-holds" in comments[0]["body"]
    assert "expectation_not_met" in comments[0]["body"]
    assert "waits for a person" in comments[2]["body"]
    assert _events(sync_engine, "task.completed", task["id"]) == []
    attempts = await _verifications(client, key, task["id"])
    assert [(a["attempt"], a["status"]) for a in attempts] == [
        (3, "failed"),
        (2, "failed"),
        (1, "failed"),
    ]
    assert attempts[0]["results"][0]["reason"] == "expectation_not_met"

    # Pages newest first, the attempt number as the cursor.
    page = (
        await client.get(f"/api/v1/tasks/{task['id']}/verifications?limit=2", headers=auth(key))
    ).json()
    assert [a["attempt"] for a in page["items"]] == [3, 2]
    rest = (
        await client.get(
            f"/api/v1/tasks/{task['id']}/verifications?limit=2&cursor={page['nextCursor']}",
            headers=auth(key),
        )
    ).json()
    assert [a["attempt"] for a in rest["items"]] == [1]
    assert rest["nextCursor"] is None


async def test_a_check_skill_without_a_result_fails_with_no_result(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["admin_key"]
    task = await create_task(client, key, title="Nobody answers", acceptance=[CHECK])
    assert (await _complete(client, key, task["id"])).status_code == 200
    await worker.run_once()

    # Nobody executes the call for longer than verification_skill_timeout.
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE skill_invocations SET created_at = now() - interval '1 day'"))
    _make_due(sync_engine)
    await worker.run_once()

    failed = _events(sync_engine, "task.verification_failed", task["id"])
    assert [f["reason"] for f in failed] == ["no_result"]
    with sync_engine.connect() as conn:
        status = conn.execute(text("SELECT status FROM skill_invocations")).scalar()
    assert status == "cancelled"
    assert (await _task(client, key, task["id"]))["systemStatusCategory"] != "terminal_success"


async def test_cancelling_the_task_cancels_its_attempt(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["admin_key"]
    task = await create_task(client, key, title="Called off", acceptance=[CHECK])
    assert (await _complete(client, key, task["id"])).status_code == 200
    await worker.run_once()

    cancelled = await _patch(client, key, task["id"], status="cancelled")
    assert cancelled["verification"]["status"] == "cancelled"
    with sync_engine.connect() as conn:
        status = conn.execute(text("SELECT status FROM skill_invocations")).scalar()
    assert status == "cancelled"

    _make_due(sync_engine)
    await worker.run_once()
    assert _events(sync_engine, "task.verified", task["id"]) == []
    assert _events(sync_engine, "task.verification_failed", task["id"]) == []
    attempts = await _verifications(client, key, task["id"])
    assert [a["status"] for a in attempts] == ["cancelled"]


# --- evidence and the registry ---------------------------------------------------


async def test_an_external_state_check_passes_by_evidence_tied_to_it(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["admin_key"]
    check = {
        "key": "state-reached",
        "kind": "external_state",
        "description": "The state is observed",
        "spec": {"event": "sample_state"},
    }
    task = await create_task(client, key, title="Waits for a fact", acceptance=[check])
    assert (await _complete(client, key, task["id"])).status_code == 200
    await worker.run_once()
    assert (await _verifications(client, key, task["id"]))[0]["status"] == "waiting_external"

    async def observe(kind: str) -> str:
        response = await client.post(
            "/api/v1/observations",
            json={"kind": kind, "content": kind, "source": "sample", "dedupKey": kind},
            headers=auth(key),
        )
        assert response.status_code == 201, response.text
        observation_id: str = response.json()["id"]
        return observation_id

    # A fact of another kind does not count.
    other = await observe("other_state")
    await _patch(
        client,
        key,
        task["id"],
        evidence=[{"kind": "observation", "observationId": other, "check": "state-reached"}],
    )
    _make_due(sync_engine)
    await worker.run_once()
    assert (await _verifications(client, key, task["id"]))[0]["status"] == "waiting_external"

    reached = await observe("sample_state")
    await _patch(
        client,
        key,
        task["id"],
        evidence=[
            {"kind": "observation", "observationId": other, "check": "state-reached"},
            {"kind": "observation", "observationId": reached, "check": "state-reached"},
        ],
    )
    _make_due(sync_engine)
    await worker.run_once()
    attempt = (await _verifications(client, key, task["id"]))[0]
    assert attempt["status"] == "passed"
    assert attempt["results"][0]["evidence"] == [{"kind": "observation", "observationId": reached}]
    assert (await _task(client, key, task["id"]))["status"] == "done"


async def test_a_deterministic_check_names_a_registered_skill_that_writes_nothing_outside(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    key = s["admin_key"]
    await _publish(client, key, name="check.writer", side_effects="external_write")
    for skill in ("check.missing@1", "check.writer@1"):
        response = await client.post(
            "/api/v1/tasks",
            json={
                "title": "Bad check",
                "acceptance": [{**CHECK, "spec": {**CHECK["spec"], "skill": skill}}],
            },
            headers=auth(key),
        )
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["code"] == "invalid_acceptance_spec"
        assert error["details"]["field"] == "acceptance[0].spec.skill"


# --- human, llm_judge: a decision on a gate approval (T003) -----------------------

HUMAN = {
    "key": "looked-at",
    "kind": "human",
    "description": "A person has looked at the result",
}


async def _decide(
    client: httpx.AsyncClient, key: str, approval_id: str, verb: str, comment: str | None = None
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/approvals/{approval_id}:{verb}",
        json={} if comment is None else {"comment": comment},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    decided: dict[str, Any] = response.json()
    return decided


async def _approval(client: httpx.AsyncClient, key: str, approval_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/approvals/{approval_id}", headers=auth(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _reviewer(
    client: httpx.AsyncClient, key: str, name: str = "reviewer", kind: str = "agent"
) -> tuple[str, str]:
    principal, reviewer_key = await create_agent_with_key(
        client,
        key,
        name=name,
        permissions=["approvals.decide", "approvals.read", "tasks.read"],
        kind=kind,
    )
    return principal["id"], reviewer_key


async def test_a_human_check_asks_its_approver_and_passes_by_the_approval(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """Scenario 4: the attempt asks a person, the decision completes the task."""
    s = await _setup(client)
    key = s["admin_key"]
    reviewer, reviewer_key = await _reviewer(client, key)
    judge, judge_key = await _reviewer(client, key, name="judge")
    judged = {
        "key": "reads-well",
        "kind": "llm_judge",
        "description": "The result reads well",
        "spec": {"approver": judge, "rubric": "Clear, short, no jargon."},
    }
    task = await create_task(
        client,
        key,
        title="Needs a look",
        acceptance=[CHECK, {**HUMAN, "spec": {"approver": reviewer}}, judged],
    )
    assert (await _complete(client, key, task["id"])).status_code == 200
    await _verify(client, worker, sync_engine, s["executor_key"], {"ok": True})

    # The deterministic check passed; the human one asked its approver and waits.
    attempt = (await _verifications(client, key, task["id"]))[0]
    assert attempt["status"] == "waiting_human"
    assert attempt["nextCheckAt"] is None
    assert [r["status"] for r in attempt["results"]] == ["passed"]
    first = await _approval(client, key, attempt["approvalId"])
    assert (first["status"], first["gate"], first["assignedPrincipalId"]) == (
        "pending",
        True,
        reviewer,
    )
    assert "looked-at" in first["comment"]
    claimability = (
        await client.get(f"/api/v1/tasks/{task['id']}/claimability", headers=auth(key))
    ).json()
    assert claimability["claimable"] is False
    # Nothing on a timer: a pass without a decision changes nothing.
    await worker.run_once()
    assert (await _verifications(client, key, task["id"]))[0]["approvalId"] == first["id"]

    # The decision wakes the attempt; the llm_judge check asks its own approver.
    await _decide(client, reviewer_key, first["id"], "approve")
    await worker.run_once()
    attempt = (await _verifications(client, key, task["id"]))[0]
    assert attempt["status"] == "waiting_human"
    assert attempt["results"][1] == {
        "key": "looked-at",
        "kind": "human",
        "source": "task",
        "status": "passed",
        "evidence": [{"kind": "approval", "ref": first["id"]}],
        "reason": None,
    }
    second = await _approval(client, key, attempt["approvalId"])
    assert second["id"] != first["id"]
    assert second["assignedPrincipalId"] == judge
    # The rubric is shown to whoever decides.
    assert "Clear, short, no jargon." in second["comment"]
    assert (await _task(client, key, task["id"]))["systemStatusCategory"] != "terminal_success"

    await _decide(client, judge_key, second["id"], "approve")
    await worker.run_once()
    attempt = (await _verifications(client, key, task["id"]))[0]
    assert attempt["status"] == "passed"
    assert attempt["results"][2]["evidence"] == [{"kind": "approval", "ref": second["id"]}]
    assert (await _task(client, key, task["id"]))["status"] == "done"
    verified = _events(sync_engine, "task.verified", task["id"])
    assert [r["status"] for r in verified[0]["results"]] == ["passed"] * 3


async def test_a_rejected_decision_fails_the_check_with_its_comment(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["admin_key"]
    owner, owner_key = await _reviewer(client, key, name="owner", kind="human")
    # No approver named: the task's owner is asked.
    task = await create_task(client, key, title="Not good yet", ownerId=owner, acceptance=[HUMAN])
    assert (await _complete(client, key, task["id"])).status_code == 200
    await worker.run_once()
    approval_id = (await _verifications(client, key, task["id"]))[0]["approvalId"]
    assert (await _approval(client, key, approval_id))["assignedPrincipalId"] == owner

    await _decide(client, owner_key, approval_id, "reject", comment="The summary is missing")
    await worker.run_once()
    attempt = (await _verifications(client, key, task["id"]))[0]
    assert attempt["status"] == "failed"
    result = attempt["results"][0]
    assert result["reason"] == "approval_rejected"
    assert "The summary is missing" in result["message"]
    assert result["evidence"] == [{"kind": "approval", "ref": approval_id}]
    failed = _events(sync_engine, "task.verification_failed", task["id"])
    assert [(f["failedCheck"], f["reason"]) for f in failed] == [("looked-at", "approval_rejected")]
    current = await _task(client, key, task["id"])
    assert current["status"] == "todo"
    comments = (await client.get(f"/api/v1/tasks/{task['id']}/comments", headers=auth(key))).json()
    assert "The summary is missing" in comments["items"][0]["body"]

    # The next attempt asks again: the rejection decided the previous one only.
    await _patch(client, key, task["id"], status="in_progress")
    assert (await _complete(client, key, task["id"])).status_code == 200
    await worker.run_once()
    again = (await _verifications(client, key, task["id"]))[0]
    assert (again["attempt"], again["status"]) == (2, "waiting_human")
    assert again["approvalId"] not in (None, approval_id)


async def test_a_human_check_falls_back_to_the_assignee_and_fails_with_nobody_to_ask(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["admin_key"]
    assignee, _ = await _reviewer(client, key, name="assignee", kind="human")
    runner, _ = await _reviewer(client, key, name="runner")
    asked = await create_task(
        client, key, title="Assigned", assigneeId=assignee, acceptance=[HUMAN]
    )
    # An agent owner is passed over for the person assigned.
    past_the_agent = await create_task(
        client, key, title="Agent-owned", ownerId=runner, assigneeId=assignee, acceptance=[HUMAN]
    )
    alone = await create_task(client, key, title="Nobody's", acceptance=[HUMAN])
    for task in (asked, past_the_agent, alone):
        assert (await _complete(client, key, task["id"])).status_code == 200
    await worker.run_once()

    for task in (asked, past_the_agent):
        attempt = (await _verifications(client, key, task["id"]))[0]
        approval = await _approval(client, key, attempt["approvalId"])
        assert approval["assignedPrincipalId"] == assignee
    attempt = (await _verifications(client, key, alone["id"]))[0]
    assert attempt["status"] == "failed"
    assert attempt["results"][0]["reason"] == "no_approver"


async def test_an_agent_executor_is_never_asked_to_accept_its_own_work(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """Without an approver and a person to ask, the check fails ``no_approver``.

    The runner that did the work is the task's assignee; it is not a fallback
    decider of its acceptance, and no approval is filed for it.
    """
    s = await _setup(client)
    key = s["admin_key"]
    runner, _ = await _reviewer(client, key, name="runner")
    task = await create_task(client, key, title="Runner's", assigneeId=runner, acceptance=[HUMAN])
    assert (await _complete(client, key, task["id"])).status_code == 200
    await worker.run_once()

    attempt = (await _verifications(client, key, task["id"]))[0]
    assert attempt["status"] == "failed"
    assert attempt["results"][0]["reason"] == "no_approver"
    assert attempt["approvalId"] is None
    with sync_engine.connect() as conn:
        filed = conn.execute(
            text("SELECT count(*) FROM approvals WHERE task_id = :task"), {"task": task["id"]}
        ).scalar_one()
    assert filed == 0


async def test_completing_again_while_the_attempt_is_open_changes_nothing(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """FR-011, SC-004: the open attempt is the answer; no second one, no events."""
    s = await _setup(client)
    key = s["admin_key"]
    waits = {"key": "fact", "kind": "external_state", "description": "A fact arrives"}
    task = await create_task(client, key, title="Handed in twice", acceptance=[waits])
    assert (await _complete(client, key, task["id"])).status_code == 200
    await worker.run_once()
    assert (await _verifications(client, key, task["id"]))[0]["status"] == "waiting_external"

    def journal() -> list[tuple[str, int]]:
        with sync_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT event_type, sequence FROM events WHERE entity_id = :task "
                    "ORDER BY sequence"
                ),
                {"task": task["id"]},
            ).all()
        return [(row.event_type, row.sequence) for row in rows]

    before = journal()
    version = (await _task(client, key, task["id"]))["version"]
    for _ in range(2):
        again = await _complete(client, key, task["id"])
        assert again.status_code == 200, again.text
    assert journal() == before
    attempts = await _verifications(client, key, task["id"])
    assert [(a["attempt"], a["status"]) for a in attempts] == [(1, "waiting_external")]
    assert (await _task(client, key, task["id"]))["version"] == version


async def test_an_approver_role_is_asked_as_a_role(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["admin_key"]
    role = await create_role(client, key, "sample-reviewers")
    task = await create_task(
        client, key, title="Role", acceptance=[{**HUMAN, "spec": {"approverRole": role["id"]}}]
    )
    assert (await _complete(client, key, task["id"])).status_code == 200
    await worker.run_once()
    attempt = (await _verifications(client, key, task["id"]))[0]
    approval = await _approval(client, key, attempt["approvalId"])
    assert (approval["requiredRoleId"], approval["assignedPrincipalId"]) == (role["id"], None)


async def test_an_approval_that_completes_the_task_counts_for_its_human_checks(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """The decision whose ``completeTask`` handed the task in is the person's word."""
    s = await _setup(client)
    key = s["admin_key"]
    created = await client.post(
        "/api/v1/task-types",
        json={
            "key": "sample-gated",
            "displayName": "sample-gated",
            "approvalSchema": {
                "gates": {"default": {"outcomes": {"approved": [{"completeTask": {}}]}}}
            },
        },
        headers=auth(key),
    )
    assert created.status_code == 201, created.text
    judged = {"key": "reads-well", "kind": "llm_judge", "description": "Reads well"}
    task = await create_task(
        client, key, title="Gated", typeKey="sample-gated", acceptance=[HUMAN, judged]
    )
    approval = await client.post(
        "/api/v1/approvals",
        json={"task": task["id"], "assignedPrincipalId": s["admin_id"], "gate": True},
        headers=auth(key),
    )
    assert approval.status_code == 201, approval.text
    approval_id = approval.json()["id"]
    await _decide(client, key, approval_id, "approve")

    # One pass runs the outcome (opens the attempt), the next runs the checks.
    await worker.run_once()
    _make_due(sync_engine)
    await worker.run_once()
    attempt = (await _verifications(client, key, task["id"]))[0]
    assert (attempt["trigger"], attempt["triggerRef"]) == ("approval", approval_id)
    assert attempt["status"] == "passed"
    assert [r["evidence"] for r in attempt["results"]] == [
        [{"kind": "approval", "ref": approval_id}]
    ] * 2
    assert (await _task(client, key, task["id"]))["status"] == "done"
    # No second decision was asked for.
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM approvals")).scalar() == 1


async def test_cancelling_the_task_withdraws_the_decision_it_asked_for(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["admin_key"]
    reviewer, _ = await _reviewer(client, key)
    task = await create_task(
        client, key, title="Called off", acceptance=[{**HUMAN, "spec": {"approver": reviewer}}]
    )
    assert (await _complete(client, key, task["id"])).status_code == 200
    await worker.run_once()
    approval_id = (await _verifications(client, key, task["id"]))[0]["approvalId"]

    cancelled = await _patch(client, key, task["id"], status="cancelled")
    assert cancelled["verification"]["status"] == "cancelled"
    assert (await _approval(client, key, approval_id))["status"] == "cancelled"
