"""M1.6 rules that close work (CP-ADR-0063, amendment 2026-09-25; CP-ADR-0067).

What is under test:

*Done is done, not cancelled.* A pair of rules on one key — one files the
work when a fact appears, the other closes it with ``complete_work`` when the
fact is gone because the work was done — leaves ONE task, done and verified,
with the second observation as its evidence. The same observation again
changes nothing.

*A rule gives the work its acceptance.* ``ensure_work.acceptance`` is the
acceptance of the task it files; ``complete_work.check`` ties the evidence to
one of those checks, which the verification stage then passes.

*Work under a live claim.* A closing decision on work somebody is running
asks the run to stop in the same pass and waits; once the claim has ended it
is applied exactly once, and never over what the executor finished first.

Observation kinds are neutral (``sample.*``): core knows no domain.
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
    create_task,
    do_bootstrap,
    open_session,
)

APPEARED = "sample.appeared"
RESOLVED = "sample.resolved"
DEDUP = "sample:{{payload.data.id}}"

FILE_RULE: dict[str, Any] = {
    "key": "sample-appeared",
    "trigger": {"kind": "observation", "type": APPEARED},
    "action": {
        "kind": "ensure_work",
        "taskType": "task",
        "dedupKeyTemplate": DEDUP,
        "fields": {"title": "Sample {{payload.data.id}} needs work"},
    },
}
COMPLETE_RULE: dict[str, Any] = {
    "key": "sample-resolved",
    "trigger": {"kind": "observation", "type": RESOLVED},
    "action": {"kind": "complete_work", "dedupKeyTemplate": DEDUP},
}
CANCEL_RULE: dict[str, Any] = {
    "key": "sample-withdrawn",
    "trigger": {"kind": "observation", "type": "sample.withdrawn"},
    "action": {"kind": "cancel_work", "dedupKeyTemplate": DEDUP},
}
RUNNER_PERMISSIONS = ["sessions.open", "tasks.read", "tasks.write", "tasks.claim"]


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


async def _admin(client: httpx.AsyncClient) -> str:
    key: str = (await do_bootstrap(client))["apiKey"]["key"]
    return key


async def _rule(client: httpx.AsyncClient, key: str, body: dict[str, Any]) -> dict[str, Any]:
    response = await client.post("/api/v1/rules", json=body, headers=auth(key))
    assert response.status_code == 201, response.text
    rule: dict[str, Any] = response.json()
    return rule


async def _observe(client: httpx.AsyncClient, key: str, kind: str, **data: Any) -> str:
    response = await client.post(
        "/api/v1/observations",
        json={"kind": kind, "content": f"{kind} seen", "data": data},
        headers=auth(key),
    )
    assert response.status_code in (200, 201), response.text
    observation_id: str = response.json()["id"]
    return observation_id


async def _evaluations(client: httpx.AsyncClient, key: str, rule_id: str) -> list[dict[str, Any]]:
    response = await client.get(
        f"/api/v1/rules/{rule_id}/evaluations", params={"limit": 100}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def _task(client: httpx.AsyncClient, key: str, ref: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{ref}", headers=auth(key))
    assert response.status_code == 200, response.text
    task: dict[str, Any] = response.json()
    return task


async def _verifications(client: httpx.AsyncClient, key: str, ref: str) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/tasks/{ref}/verifications", headers=auth(key))
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def _rule_task_ids(sync_engine: Engine) -> list[str]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id FROM tasks WHERE origin->>'kind' = 'rule' ORDER BY created_at")
        ).all()
    return [str(row.id) for row in rows]


def _count(sync_engine: Engine, event_type: str, entity_id: str | None = None) -> int:
    with sync_engine.connect() as conn:
        count: int = conn.execute(
            text(
                "SELECT count(*) FROM events WHERE event_type = :type "
                "AND (CAST(:entity AS uuid) IS NULL OR entity_id = CAST(:entity AS uuid))"
            ),
            {"type": event_type, "entity": entity_id},
        ).scalar_one()
    return count


def _attempts(sync_engine: Engine) -> int:
    with sync_engine.connect() as conn:
        count: int = conn.execute(text("SELECT count(*) FROM task_verifications")).scalar_one()
    return count


def _make_waiting_due(sync_engine: Engine) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE rule_evaluations SET next_check_at = now() WHERE status = 'waiting'")
        )


async def _run_on(
    client: httpx.AsyncClient, admin_key: str, task_id: str
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """An executor claims the task and starts a run on it."""
    _, runner_key = await create_agent_with_key(
        client, admin_key, name="runner", permissions=RUNNER_PERMISSIONS
    )
    session = await open_session(client, runner_key)
    claimed = await claim_task(client, runner_key, task_id, session["id"])
    assert claimed.status_code == 200, claimed.text
    claim: dict[str, Any] = claimed.json()
    started = await client.post(
        f"/api/v1/tasks/{task_id}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(runner_key),
    )
    assert started.status_code in (200, 201), started.text
    run: dict[str, Any] = started.json()
    return runner_key, claim, run


# --- complete_work ----------------------------------------------------------------


async def test_a_pair_of_rules_closes_the_work_as_done_on_two_observations(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """SC-003: the work whose premise was met is done and verified, not cancelled."""
    key = await _admin(client)
    await _rule(client, key, FILE_RULE)
    closing = await _rule(client, key, COMPLETE_RULE)

    await _observe(client, key, APPEARED, id="a")
    await worker.run_once()
    [task_id] = _rule_task_ids(sync_engine)

    resolved = await _observe(client, key, RESOLVED, id="a")
    # One cycle: the rule closes the work, the verification pass verifies it.
    await worker.run_once()
    task = await _task(client, key, task_id)
    assert (task["status"], task["systemStatusCategory"]) == ("done", "terminal_success")
    # The decision is recorded on the work itself (FR-009).
    assert task["evidence"] == [
        {"kind": "observation", "observationId": resolved, "check": "rule-evidence"}
    ]
    assert task["acceptance"] == []
    [evaluation] = await _evaluations(client, key, closing["id"])
    assert evaluation["status"] == "matched"
    [work] = evaluation["result"]["work"]
    assert (work["action"], work["taskId"], work["check"]) == (
        "complete_work",
        task_id,
        "rule-evidence",
    )
    [attempt] = await _verifications(client, key, task_id)
    assert (attempt["status"], attempt["trigger"], attempt["triggerRef"]) == (
        "passed",
        "rule",
        f"rule_evaluation:{evaluation['id']}",
    )
    assert attempt["id"] == work["verificationId"]
    # No acceptance of its own: the implicit check, passed by the rule's fact.
    assert attempt["results"] == [
        {
            "key": "rule-evidence",
            "kind": "external_state",
            "source": "rule",
            "status": "passed",
            "evidence": [{"kind": "observation", "observationId": resolved}],
            "reason": None,
        }
    ]
    assert _count(sync_engine, "task.completed", task_id) == 1
    assert _count(sync_engine, "task.verified", task_id) == 1
    assert _count(sync_engine, "work.reconciled", task_id) == 1

    # The same fact again: the work is closed, nothing changes (FR-011).
    await _observe(client, key, RESOLVED, id="a")
    await worker.run_once()
    await worker.run_once()
    latest = (await _evaluations(client, key, closing["id"]))[0]
    assert latest["result"]["work"][0]["reason"] == "no_open_work"
    assert _rule_task_ids(sync_engine) == [task_id]
    assert _attempts(sync_engine) == 1
    assert _count(sync_engine, "task.completed", task_id) == 1
    assert _count(sync_engine, "task.verified", task_id) == 1
    assert _count(sync_engine, "work.reconciled", task_id) == 1
    assert (await _task(client, key, task_id))["evidence"] == task["evidence"]


async def test_a_rule_gives_its_work_acceptance_and_completes_it_by_a_named_check(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    key = await _admin(client)
    await _rule(
        client,
        key,
        {
            **FILE_RULE,
            "action": {
                **FILE_RULE["action"],
                "acceptance": [
                    {
                        "key": "resolved",
                        "kind": "external_state",
                        "description": "Sample {{payload.data.id}} is resolved",
                        "spec": {"event": RESOLVED},
                    }
                ],
            },
        },
    )
    await _rule(
        client, key, {**COMPLETE_RULE, "action": {**COMPLETE_RULE["action"], "check": "resolved"}}
    )

    await _observe(client, key, APPEARED, id="b")
    await worker.run_once()
    [task_id] = _rule_task_ids(sync_engine)
    task = await _task(client, key, task_id)
    assert task["acceptance"] == [
        {
            "key": "resolved",
            "kind": "external_state",
            "description": "Sample b is resolved",
            "spec": {"event": RESOLVED},
        }
    ]

    resolved = await _observe(client, key, RESOLVED, id="b")
    await worker.run_once()
    task = await _task(client, key, task_id)
    assert task["status"] == "done"
    assert {"kind": "observation", "observationId": resolved, "check": "resolved"} in task[
        "evidence"
    ]
    [attempt] = await _verifications(client, key, task_id)
    assert [(r["key"], r["status"]) for r in attempt["results"]] == [("resolved", "passed")]


async def test_a_rule_fed_evidence_wakes_an_attempt_that_waits_for_it(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """Work handed in and waiting for a fact keeps its attempt; the rule supplies the fact."""
    key = await _admin(client)
    await _rule(
        client,
        key,
        {
            **FILE_RULE,
            "action": {
                **FILE_RULE["action"],
                "acceptance": [
                    {"key": "resolved", "kind": "external_state", "description": "Resolved"}
                ],
            },
        },
    )
    await _rule(
        client, key, {**COMPLETE_RULE, "action": {**COMPLETE_RULE["action"], "check": "resolved"}}
    )
    await _observe(client, key, APPEARED, id="c")
    await worker.run_once()
    [task_id] = _rule_task_ids(sync_engine)
    task = await _task(client, key, task_id)
    handed_in = await client.post(
        f"/api/v1/tasks/{task_id}:complete",
        headers={**auth(key), "If-Match": f'"task-{task["version"]}"'},
    )
    assert handed_in.status_code == 200, handed_in.text
    await worker.run_once()
    [attempt] = await _verifications(client, key, task_id)
    assert attempt["status"] == "waiting_external"

    await _observe(client, key, RESOLVED, id="c")
    await worker.run_once()
    [attempt] = await _verifications(client, key, task_id)
    assert (attempt["status"], attempt["trigger"]) == ("passed", "complete")
    assert (await _task(client, key, task_id))["status"] == "done"


async def test_cancel_work_writes_its_evidence_into_the_task(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    key = await _admin(client)
    await _rule(client, key, FILE_RULE)
    await _rule(client, key, CANCEL_RULE)
    await _observe(client, key, APPEARED, id="d")
    await worker.run_once()
    [task_id] = _rule_task_ids(sync_engine)
    withdrawn = await _observe(client, key, "sample.withdrawn", id="d")
    await worker.run_once()
    task = await _task(client, key, task_id)
    assert task["systemStatusCategory"] == "terminal_cancelled"
    assert task["evidence"] == [{"kind": "observation", "observationId": withdrawn}]
    assert await _verifications(client, key, task_id) == []


async def test_rule_conditions_see_the_verification_of_the_task(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """``task.verification`` tells work being checked from abandoned work (A5)."""
    key = await _admin(client)
    await _rule(
        client,
        key,
        {
            "key": "sample-checked",
            "trigger": {"kind": "event", "type": "task.verification_started"},
            "condition": {
                "and": [
                    {"eq": [{"var": "task.verification.status"}, "running"]},
                    {"eq": [{"var": "task.verification.attempt"}, 1]},
                ]
            },
            "action": {
                "kind": "ensure_work",
                "taskType": "task",
                "dedupKeyTemplate": "checked:{{task.id}}",
                "fields": {"title": "{{task.publicId}} is being checked"},
            },
        },
    )
    checked = await create_task(
        client,
        key,
        title="Checked",
        acceptance=[{"key": "fact", "kind": "external_state", "description": "A fact"}],
    )
    response = await client.post(
        f"/api/v1/tasks/{checked['id']}:complete",
        headers={**auth(key), "If-Match": f'"task-{checked["version"]}"'},
    )
    assert response.status_code == 200, response.text
    await worker.run_once()
    [derived] = _rule_task_ids(sync_engine)
    assert (await _task(client, key, derived))["title"] == f"{checked['publicId']} is being checked"


# --- work under a live claim ------------------------------------------------------


async def test_a_closing_rule_on_claimed_work_stops_the_run_and_applies_once(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """SC-006: cancelRequestedAt in the same pass; the decision applied once, after."""
    key = await _admin(client)
    await _rule(client, key, FILE_RULE)
    cancel = await _rule(client, key, CANCEL_RULE)
    await _observe(client, key, APPEARED, id="e")
    await worker.run_once()
    [task_id] = _rule_task_ids(sync_engine)
    runner_key, claim, run = await _run_on(client, key, task_id)

    withdrawn = await _observe(client, key, "sample.withdrawn", id="e")
    await worker.run_once()
    current = (await client.get(f"/api/v1/runs/{run['id']}", headers=auth(key))).json()
    assert current["cancelRequestedAt"] is not None
    assert current["status"] == "running"
    [evaluation] = await _evaluations(client, key, cancel["id"])
    assert evaluation["status"] == "waiting"
    [work] = evaluation["result"]["work"]
    assert (work["waiting"], work["reason"], work["runId"], work["cancelRequested"]) == (
        True,
        "task_claimed",
        run["id"],
        True,
    )
    task = await _task(client, key, task_id)
    assert task["systemStatusCategory"] == "active"
    assert _count(sync_engine, "run.cancel_requested", run["id"]) == 1

    # Still running: the decision keeps waiting, the run is not asked twice.
    _make_waiting_due(sync_engine)
    await worker.run_once()
    assert (await _evaluations(client, key, cancel["id"]))[0]["status"] == "waiting"
    assert _count(sync_engine, "run.cancel_requested", run["id"]) == 1

    # The executor stops and lets go.
    stopped = await client.post(
        f"/api/v1/runs/{run['id']}:cancel", json={"reason": "asked"}, headers=auth(runner_key)
    )
    assert stopped.status_code == 200, stopped.text
    released = await client.post(
        f"/api/v1/claims/{claim['id']}:release",
        json={"reason": "cancelled"},
        headers=auth(runner_key),
    )
    assert released.status_code == 200, released.text

    _make_waiting_due(sync_engine)
    await worker.run_once()
    [evaluation] = await _evaluations(client, key, cancel["id"])
    assert evaluation["status"] == "matched"
    [work] = evaluation["result"]["work"]
    assert (work["afterClaim"], work["changes"]) == (True, ["evidence", "status"])
    assert "waitingFor" not in evaluation["result"]
    task = await _task(client, key, task_id)
    assert task["systemStatusCategory"] == "terminal_cancelled"
    assert task["evidence"] == [{"kind": "observation", "observationId": withdrawn}]

    # Exactly once: another pass finds nothing to do.
    _make_waiting_due(sync_engine)
    await worker.run_once()
    await worker.run_once()
    assert _count(sync_engine, "work.reconciled", task_id) == 1
    assert _count(sync_engine, "rule.evaluated", cancel["id"]) == 1
    assert (await _task(client, key, task_id))["version"] == task["version"]


async def test_a_decision_waiting_on_a_claim_does_not_overwrite_finished_work(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """The executor finished first: its result stands (``already_done``)."""
    key = await _admin(client)
    await _rule(client, key, FILE_RULE)
    cancel = await _rule(client, key, CANCEL_RULE)
    await _observe(client, key, APPEARED, id="f")
    await worker.run_once()
    [task_id] = _rule_task_ids(sync_engine)
    runner_key, _, run = await _run_on(client, key, task_id)

    await _observe(client, key, "sample.withdrawn", id="f")
    await worker.run_once()
    assert (await _evaluations(client, key, cancel["id"]))[0]["status"] == "waiting"

    finished = await client.post(
        f"/api/v1/runs/{run['id']}:succeed", json={}, headers=auth(runner_key)
    )
    assert finished.status_code == 200, finished.text
    assert (await _task(client, key, task_id))["status"] == "done"

    _make_waiting_due(sync_engine)
    await worker.run_once()
    [evaluation] = await _evaluations(client, key, cancel["id"])
    assert evaluation["status"] == "matched"
    assert evaluation["result"]["work"][0]["reason"] == "already_done"
    task = await _task(client, key, task_id)
    assert (task["status"], task["evidence"]) == ("done", [])
    assert _count(sync_engine, "work.reconciled", task_id) == 0


async def test_a_claim_that_is_never_released_fails_the_decision(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    key = await _admin(client)
    await _rule(client, key, FILE_RULE)
    complete = await _rule(client, key, COMPLETE_RULE)
    await _observe(client, key, APPEARED, id="g")
    await worker.run_once()
    [task_id] = _rule_task_ids(sync_engine)
    # A person's harness: a claim without a run, nobody to ask to stop.
    _, person_key = await create_agent_with_key(
        client, key, name="person", permissions=RUNNER_PERMISSIONS, kind="human"
    )
    session = await open_session(client, person_key)
    assert (await claim_task(client, person_key, task_id, session["id"])).status_code == 200

    await _observe(client, key, RESOLVED, id="g")
    await worker.run_once()
    [evaluation] = await _evaluations(client, key, complete["id"])
    assert evaluation["status"] == "waiting"
    work = evaluation["result"]["work"][0]
    assert "runId" not in work and "cancelRequested" not in work

    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE rule_evaluations SET next_check_at = now(), result = jsonb_set("
                "result, '{waitingSince}', '\"2000-01-01T00:00:00+00:00\"') "
                "WHERE status = 'waiting'"
            )
        )
    await worker.run_once()
    [evaluation] = await _evaluations(client, key, complete["id"])
    assert evaluation["status"] == "failed"
    assert evaluation["error"]["code"] == "claim_not_released"
    assert evaluation["result"]["work"][0]["reason"] == "claim_not_released"
    assert (await _task(client, key, task_id))["systemStatusCategory"] == "active"
