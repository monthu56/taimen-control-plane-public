"""Approval outcomes declared by the task type (CP-ADR-0061, TAI-ADR-0041).

The loop under test is the one the code-review type exists for: a human
decides the gate approval on a review task, and core — not the runner, not a
script — does what the type version declares: on reject a work item with the
fixes for the coder and the review closed, on approve the review closed.
Everything is driven through the API and the worker, the way it runs.
"""

import importlib.util
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from platform_auth import ObjectPage, PolicyDecision
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.authorization import Authorizer, configure_authorizer
from control_plane.application.commands import approval_outcomes as outcomes_module
from control_plane.config import Settings
from control_plane.worker.main import Worker
from tests.helpers import (
    auth,
    claim_task,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
    open_session,
)


def _migration_schema() -> dict[str, Any]:
    """The code-review v3 document exactly as the data migration ships it."""
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/e3b8c1a6d9f4_approval_outcomes.py"
    )
    spec = importlib.util.spec_from_file_location("approval_outcomes_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema: dict[str, Any] = module.CODE_REVIEW_APPROVAL_SCHEMA
    return schema


CODE_REVIEW_SCHEMA = _migration_schema()
# artifacts.read: code-review v3 copies the branch from the source's commit
# artifact, and what an outcome copies its decider must be able to read.
REVIEWER_PERMISSIONS = [
    "approvals.read",
    "approvals.decide",
    "tasks.read",
    "tasks.write",
    "artifacts.read",
]


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


async def _create_type(
    client: httpx.AsyncClient, key: str, type_key: str, **extra: Any
) -> httpx.Response:
    return await client.post(
        "/api/v1/task-types",
        json={"key": type_key, "displayName": type_key, **extra},
        headers=auth(key),
    )


async def _review_setup(
    client: httpx.AsyncClient,
    *,
    reviewer_permissions: list[str] | None = None,
    schema: dict[str, Any] | None = None,
    gate: bool = True,
    spawned: bool = True,
    assigned: bool = True,
    commit: bool = True,
    coding_extra: dict[str, Any] | None = None,
    admin_key: str | None = None,
) -> dict[str, Any]:
    """Coder's finished task with a published branch, and a gated review on it.

    The switches take pieces away: the gate (to claim the review first), the
    ``spawned_by`` edge, the coder as assignee, the commit artifact.
    """
    admin_key = admin_key or (await do_bootstrap(client))["apiKey"]["key"]
    assert (await _create_type(client, admin_key, "coding-task")).status_code == 201
    created = await _create_type(
        client, admin_key, "code-review", approvalSchema=schema or CODE_REVIEW_SCHEMA
    )
    assert created.status_code == 201, created.text

    coder, _ = await create_agent_with_key(client, admin_key, name="coder")
    reviewer, reviewer_key = await create_agent_with_key(
        client,
        admin_key,
        name="reviewer",
        permissions=reviewer_permissions or REVIEWER_PERMISSIONS,
    )
    coding = await create_task(
        client,
        admin_key,
        title="Implement the importer",
        description="Import everything. Idempotently.",
        typeKey="coding-task",
        priority="high",
        **({"assigneeId": coder["id"]} if assigned else {}),
        **(coding_extra or {}),
    )
    if commit:
        artifact = await client.post(
            "/api/v1/artifacts",
            json={
                "type": "commit",
                "name": "commit",
                "task": coding["id"],
                "metadata": {"branch": f"task/{coding['publicId']}", "commit": "abc123"},
            },
            headers=auth(admin_key),
        )
        assert artifact.status_code == 201, artifact.text
    review = await create_task(
        client,
        admin_key,
        title=f"Review {coding['publicId']}",
        typeKey="code-review",
        assigneeId=reviewer["id"],
    )
    if spawned:
        relation = await client.post(
            f"/api/v1/tasks/{review['id']}/relations",
            json={"toTask": coding["id"], "type": "spawned_by"},
            headers=auth(admin_key),
        )
        assert relation.status_code == 201, relation.text
    approval = await _gate(client, admin_key, review["id"], reviewer["id"]) if gate else None
    return {
        "admin_key": admin_key,
        "coder": coder,
        "reviewer": reviewer,
        "reviewer_key": reviewer_key,
        "coding": coding,
        "review": review,
        "approval": approval,
    }


async def _gate(
    client: httpx.AsyncClient, admin_key: str, task_id: str, principal_id: str
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/approvals",
        json={"task": task_id, "assignedPrincipalId": principal_id, "gate": True},
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text
    approval: dict[str, Any] = response.json()
    return approval


async def _decide(
    client: httpx.AsyncClient, key: str, approval_id: str, verb: str, comment: str | None = None
) -> dict[str, Any]:
    body = {"comment": comment} if comment is not None else {}
    response = await client.post(
        f"/api/v1/approvals/{approval_id}:{verb}", json=body, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    decided: dict[str, Any] = response.json()
    return decided


async def _outcome(client: httpx.AsyncClient, key: str, approval_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/approvals/{approval_id}/outcome", headers=auth(key))
    assert response.status_code == 200, response.text
    outcome: dict[str, Any] = response.json()
    return outcome


async def _task(client: httpx.AsyncClient, key: str, ref: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{ref}", headers=auth(key))
    assert response.status_code == 200, response.text
    task: dict[str, Any] = response.json()
    return task


def _tasks_of_type(sync_engine: Engine, type_key: str) -> list[Any]:
    with sync_engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT t.id, t.public_id, t.title, t.description, t.assignee_id, "
                    "t.priority, t.status, t.origin "
                    "FROM tasks t JOIN task_types y ON y.id = t.type_id "
                    "WHERE y.key = :key ORDER BY t.created_at"
                ),
                {"key": type_key},
            ).all()
        )


async def _events(client: httpx.AsyncClient, key: str, event_type: str) -> list[dict[str, Any]]:
    items = (
        await client.get("/api/v1/events?limit=200&entityType=approval", headers=auth(key))
    ).json()["items"]
    return [e for e in items if e["type"] == event_type]


# --- reject / approve -----------------------------------------------------------


async def test_reject_creates_exactly_one_fix_task_and_closes_the_review(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _review_setup(client)
    approval_id = s["approval"]["id"]

    decided = await _decide(
        client, s["reviewer_key"], approval_id, "reject", "Null check is missing in parse()"
    )
    assert decided["status"] == "rejected"
    assert decided["outcomeStatus"] == "pending"

    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcome"] == "rejected"
    assert outcome["outcomeStatus"] == "executed"
    assert [(a["action"], a["status"]) for a in outcome["actions"]] == [
        ("ensureWork", "executed"),
        ("completeTask", "executed"),
    ]

    coding_tasks = _tasks_of_type(sync_engine, "coding-task")
    assert len(coding_tasks) == 2
    fix = coding_tasks[1]
    source = s["coding"]
    assert fix.title == f"Правки по ревью {source['publicId']}: {source['title']}"
    assert "Null check is missing in parse()" in fix.description
    assert "Import everything. Idempotently." in fix.description
    assert f"Продолжи ветку task/{source['publicId']}" in fix.description
    assert str(fix.assignee_id) == s["coder"]["id"]
    assert fix.priority == "high"
    assert outcome["actions"][0]["result"]["taskId"] == str(fix.id)

    relations = (
        await client.get(f"/api/v1/tasks/{fix.id}/relations", headers=auth(s["admin_key"]))
    ).json()["items"]
    assert [(r["type"], r["toTaskId"]) for r in relations if r["fromTaskId"] == str(fix.id)] == [
        ("spawned_by", s["review"]["id"])
    ]
    # The origin of the new work item is the approval (CP-ADR-0061 §origin).
    with sync_engine.connect() as conn:
        origin = conn.execute(
            text(
                "SELECT external_id, metadata FROM external_references "
                "WHERE entity_id = :id AND external_type = 'approval-outcome'"
            ),
            {"id": fix.id},
        ).one()
    assert origin.external_id == f"review-fix:{approval_id}"
    assert origin.metadata["approvalId"] == approval_id
    # ...and the work graph says so too: core filed it as a process step (CP-ADR-0062).
    assert fix.origin == {"kind": "process", "ref": f"approval:{approval_id}", "evidence": []}

    review = await _task(client, s["admin_key"], s["review"]["id"])
    assert review["systemStatusCategory"] == "terminal_success"

    executed = await _events(client, s["admin_key"], "approval.outcome_executed")
    assert len(executed) == 1
    assert executed[0]["entityId"] == approval_id
    assert executed[0]["actorId"] == s["reviewer"]["id"]
    assert [a["action"] for a in executed[0]["payload"]["actions"]] == [
        "ensureWork",
        "completeTask",
    ]


async def test_redelivered_decision_repeats_nothing(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _review_setup(client)
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "reject", "Redo it")
    await worker.run_once()

    # At-least-once: the same outbox record is delivered again.
    with sync_engine.begin() as conn:
        again = conn.execute(
            text(
                "UPDATE outbox SET delivered_at = NULL WHERE topic = 'approval.rejected' "
                "RETURNING id"
            )
        ).all()
    assert len(again) == 1
    await worker.run_once()
    await worker.run_once()

    assert len(_tasks_of_type(sync_engine, "coding-task")) == 2
    with sync_engine.connect() as conn:
        attempts = conn.execute(
            text("SELECT action_index, attempts FROM approval_outcome_actions ORDER BY 1")
        ).all()
    assert [tuple(row) for row in attempts] == [(0, 1), (1, 1)]
    assert len(await _events(client, s["admin_key"], "approval.outcome_executed")) == 1


async def test_approve_completes_the_review_and_creates_no_work(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _review_setup(client)
    approval_id = s["approval"]["id"]

    await _decide(client, s["reviewer_key"], approval_id, "approve")
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "executed"
    assert [(a["action"], a["status"]) for a in outcome["actions"]] == [
        ("completeTask", "executed")
    ]
    review = await _task(client, s["admin_key"], s["review"]["id"])
    assert review["systemStatusCategory"] == "terminal_success"
    assert len(_tasks_of_type(sync_engine, "coding-task")) == 1


# --- authority, failure, replay ---------------------------------------------------


async def test_action_the_decider_may_not_perform_fails_the_outcome(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    # The reviewer may decide, but not write tasks.
    s = await _review_setup(client, reviewer_permissions=["approvals.read", "approvals.decide"])
    approval_id = s["approval"]["id"]

    await _decide(client, s["reviewer_key"], approval_id, "reject", "Needs work")
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "failed"
    first, second = outcome["actions"]
    assert (first["action"], first["status"]) == ("ensureWork", "failed")
    assert first["error"]["code"] == "forbidden"
    # The rest of the outcome is not attempted.
    assert (second["action"], second["status"]) == ("completeTask", "not_executed")

    # The decision stands, nothing the decider may not do was done.
    approval = (
        await client.get(f"/api/v1/approvals/{approval_id}", headers=auth(s["admin_key"]))
    ).json()
    assert approval["status"] == "rejected"
    assert len(_tasks_of_type(sync_engine, "coding-task")) == 1
    review = await _task(client, s["admin_key"], s["review"]["id"])
    assert review["systemStatusCategory"] != "terminal_success"

    failed = await _events(client, s["admin_key"], "approval.outcome_failed")
    assert len(failed) == 1
    payload = failed[0]["payload"]
    assert payload["failedAction"]["index"] == 0
    assert payload["failedAction"]["code"] == "forbidden"
    # A work item lands on the decider so the failure is not lost.
    triage = await _task(client, s["admin_key"], payload["failureWorkTaskId"])
    assert triage["assigneeId"] == s["reviewer"]["id"]
    assert approval_id in triage["description"]
    # ...filed by core's own service principal, not in the decider's name.
    assert triage["createdBy"] != s["reviewer"]["id"]
    core = (
        await client.get(f"/api/v1/principals/{triage['createdBy']}", headers=auth(s["admin_key"]))
    ).json()
    assert (core["kind"], core["metadata"]) == ("service", {"system": "control-plane-core"})


async def test_replay_resumes_at_the_first_action_that_did_not_execute(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _review_setup(client)
    approval_id = s["approval"]["id"]
    # A second gate on the review: completeTask will hit approval_required.
    second = await _gate(client, s["admin_key"], s["review"]["id"], s["reviewer"]["id"])

    await _decide(client, s["reviewer_key"], approval_id, "reject", "Fix it")
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "failed"
    assert [(a["action"], a["status"]) for a in outcome["actions"]] == [
        ("ensureWork", "executed"),
        ("completeTask", "failed"),
    ]
    assert outcome["actions"][1]["error"]["code"] == "approval_required"

    # Nobody but the decider (or an admin) may replay.
    _, stranger_key = await create_agent_with_key(
        client, s["admin_key"], name="stranger", permissions=REVIEWER_PERMISSIONS
    )
    refused = await client.post(
        f"/api/v1/approvals/{approval_id}:replay-outcome", json={}, headers=auth(stranger_key)
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "not_eligible"

    cancelled = await client.post(
        f"/api/v1/approvals/{second['id']}:cancel", json={}, headers=auth(s["admin_key"])
    )
    assert cancelled.status_code == 200, cancelled.text

    replayed = await client.post(
        f"/api/v1/approvals/{approval_id}:replay-outcome",
        json={},
        headers=auth(s["reviewer_key"]),
    )
    assert replayed.status_code == 200, replayed.text
    body = replayed.json()
    assert body["outcomeStatus"] == "executed"
    assert [(a["status"], a["attempts"]) for a in body["actions"]] == [
        ("executed", 1),  # not repeated
        ("executed", 2),
    ]
    assert len(_tasks_of_type(sync_engine, "coding-task")) == 2
    review = await _task(client, s["admin_key"], s["review"]["id"])
    assert review["systemStatusCategory"] == "terminal_success"

    again = await client.post(
        f"/api/v1/approvals/{approval_id}:replay-outcome",
        json={},
        headers=auth(s["reviewer_key"]),
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "outcome_not_replayable"


async def test_replay_by_the_decider_uses_their_current_authority(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _review_setup(client, reviewer_permissions=["approvals.read", "approvals.decide"])
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "reject", "Needs work")
    await worker.run_once()
    assert (await _outcome(client, s["admin_key"], approval_id))["outcomeStatus"] == "failed"

    # The fix for `forbidden`: the decider gets the right, then replays.
    granted = await client.post(
        f"/api/v1/principals/{s['reviewer']['id']}/api-keys",
        json={"permissions": REVIEWER_PERMISSIONS},
        headers=auth(s["admin_key"]),
    )
    assert granted.status_code == 201, granted.text
    replayed = await client.post(
        f"/api/v1/approvals/{approval_id}:replay-outcome",
        json={},
        headers=auth(granted.json()["key"]),
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["outcomeStatus"] == "executed"
    assert len(_tasks_of_type(sync_engine, "coding-task")) == 2


_INVOKE_SCHEMA: dict[str, Any] = {
    "gates": {
        "default": {
            "outcomes": {
                "approved": [
                    {
                        "invokeSkill": {
                            "skill": "notify@1",
                            "inputs": {"branch": "$.spawnedBy.artifact[commit].metadata.branch"},
                            "onSuccess": [{"completeTask": {}}],
                        }
                    },
                ]
            }
        }
    }
}


async def _publish_notify(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/skills",
        json={
            "name": "notify",
            "version": "1",
            "sideEffects": "none",
            "riskLevel": "low",
            "contract": {
                "inputs": {"type": "object"},
                "outputs": {"type": "object"},
                "timeoutSeconds": 10,
                "idempotency": "natural",
                "implementation": {"protocol": "local", "entrypoint": "notify:run"},
            },
        },
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    skill: dict[str, Any] = response.json()
    return skill


async def test_invoke_skill_of_an_unpublished_skill_is_refused_at_publication(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await _create_type(client, admin_key, "code-review", approvalSchema=_INVOKE_SCHEMA)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_approval_schema"
    assert error["details"]["reason"] == "not_found"


async def test_invoke_skill_of_a_disabled_skill_fails_the_outcome(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    """A skill gone since publication is a failed step with work on it, never a silent skip."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    skill = await _publish_notify(client, admin_key)
    s = await _review_setup(
        client,
        schema=_INVOKE_SCHEMA,
        reviewer_permissions=[*REVIEWER_PERMISSIONS, "skills.invoke"],
        admin_key=admin_key,
    )
    disabled = await client.patch(
        f"/api/v1/skills/{skill['id']}",
        json={"status": "disabled"},
        headers={**auth(admin_key), "If-Match": '"skill-1"'},
    )
    assert disabled.status_code == 200, disabled.text
    approval_id = s["approval"]["id"]

    await _decide(client, s["reviewer_key"], approval_id, "approve")
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "failed"
    assert outcome["actions"][0]["error"]["code"] == "skill_not_invocable"
    assert outcome["actions"][1]["status"] == "not_executed"


async def test_comment_and_transition_run_with_rendered_inputs(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    schema = {
        "gates": {
            "default": {
                "outcomes": {
                    "rejected": [
                        {
                            "comment": {
                                "body": "Rejected by $.approval.decidedBy: $.approval.comment"
                            }
                        },
                        {"transition": {"status": "blocked"}},
                        {
                            "comment": {
                                "task": "$.spawnedBy.publicId",
                                "body": "Review $.task.publicId asked for changes",
                            }
                        },
                    ]
                }
            }
        }
    }
    s = await _review_setup(client, schema=schema)
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "reject", "Too slow")
    await worker.run_once()

    assert (await _outcome(client, s["admin_key"], approval_id))["outcomeStatus"] == "executed"
    review = await _task(client, s["admin_key"], s["review"]["id"])
    assert review["status"] == "blocked"
    on_review = (
        await client.get(
            f"/api/v1/tasks/{s['review']['id']}/comments", headers=auth(s["admin_key"])
        )
    ).json()["items"]
    assert [c["body"] for c in on_review] == [f"Rejected by {s['reviewer']['id']}: Too slow"]
    assert on_review[0]["authorPrincipalId"] == s["reviewer"]["id"]
    on_source = (
        await client.get(
            f"/api/v1/tasks/{s['coding']['id']}/comments", headers=auth(s["admin_key"])
        )
    ).json()["items"]
    assert [c["body"] for c in on_source] == [f"Review {s['review']['publicId']} asked for changes"]


# --- no schema: as before ---------------------------------------------------------


async def test_type_without_approval_schema_behaves_as_before(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    reviewer, reviewer_key = await create_agent_with_key(
        client, admin_key, name="reviewer", permissions=REVIEWER_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="Plain task")
    approval = await _gate(client, admin_key, task["id"], reviewer["id"])

    decided = await _decide(client, reviewer_key, approval["id"], "reject", "No")
    assert decided["outcomeStatus"] is None
    await worker.run_once()

    outcome = await _outcome(client, admin_key, approval["id"])
    assert outcome == {
        "approvalId": approval["id"],
        "outcome": "rejected",
        "outcomeStatus": None,
        "attempts": 0,
        "lastError": None,
        "nextAttemptAt": None,
        "actions": [],
    }
    assert (await _task(client, admin_key, task["id"]))["status"] == "todo"
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM approval_outcome_actions")).scalar() == 0
    assert await _events(client, admin_key, "approval.outcome_executed") == []
    replay = await client.post(
        f"/api/v1/approvals/{approval['id']}:replay-outcome", json={}, headers=auth(reviewer_key)
    )
    assert replay.status_code == 409


async def test_plain_approval_sets_no_outcome_in_motion(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    """Only a gate approval runs the type's outcomes; a plain one is advisory."""
    s = await _review_setup(client)
    plain = await client.post(
        "/api/v1/approvals",
        json={"task": s["review"]["id"], "assignedPrincipalId": s["reviewer"]["id"]},
        headers=auth(s["admin_key"]),
    )
    assert plain.status_code == 201
    decided = await _decide(client, s["reviewer_key"], plain.json()["id"], "approve")
    assert decided["outcomeStatus"] is None


# --- publication ---------------------------------------------------------------


def _gate_doc(outcomes: dict[str, Any]) -> dict[str, Any]:
    return {"gates": {"default": {"outcomes": outcomes}}}


@pytest.mark.parametrize(
    ("schema", "fragment"),
    [
        (_gate_doc({"approved": [{"merge": {}}]}), "unknown action"),
        (_gate_doc({"approved": [{"completeTask": {"force": True}}]}), "unknown inputs"),
        (_gate_doc({"approved": [{"comment": {}}]}), "missing inputs"),
        (_gate_doc({"approved": [{"comment": {"body": "x"}, "completeTask": {}}]}), "exactly one"),
        (_gate_doc({"maybe": []}), "unknown outcomes"),
        (_gate_doc({"approved": [{"comment": {"body": "$.env.HOME"}}]}), "unknown root"),
        (_gate_doc({"approved": [{"comment": {"body": "$.task.secret"}}]}), "expected $.task"),
        (
            _gate_doc({"approved": [{"comment": {"body": "$.approval.comment.upper"}}]}),
            "expected $.approval",
        ),
        (
            _gate_doc({"approved": [{"comment": {"body": "$.spawnedBy.artifact[commit].branch"}}]}),
            "expected $.spawnedBy",
        ),
        (_gate_doc({"approved": [{"transition": {"status": "shipped"}}]}), "not declared"),
        (
            _gate_doc(
                {
                    "rejected": [
                        {
                            "ensureWork": {
                                "type": "coding-task",
                                "key": "k",
                                "title": "t",
                                "relation": {"caused_by": "$.task.id"},
                            }
                        }
                    ]
                }
            ),
            "unknown relation type",
        ),
        (
            _gate_doc({"approved": [{"invokeSkill": {"skill": "$.task.title"}}]}),
            "literal skill reference",
        ),
        ({"gates": {"Default": {"outcomes": {}}}}, "gate names"),
        # Approvals carry no gate name yet: a named gate could never fire.
        ({"gates": {"release": {"outcomes": {"approved": []}}}}, "only the 'default' gate"),
        ({"outcomes": {}}, "unknown keys"),
        # Rejecting stays possible whatever the world looks like (TAI-ADR-0041 p.7).
        ({"gates": {"default": {"preconditions": {"rejected": []}}}}, "preconditions only of"),
    ],
)
async def test_invalid_approval_schema_is_refused_at_publication(
    client: httpx.AsyncClient, schema: dict[str, Any], fragment: str
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await _create_type(client, admin_key, "code-review", approvalSchema=schema)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_approval_schema"
    assert fragment in error["message"]


async def test_valid_approval_schema_is_part_of_the_immutable_version(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await _create_type(
        client, admin_key, "code-review", approvalSchema=CODE_REVIEW_SCHEMA
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["approvalSchema"] == CODE_REVIEW_SCHEMA
    fetched = (
        await client.get(f"/api/v1/task-types/{created['id']}", headers=auth(admin_key))
    ).json()
    assert fetched["approvalSchema"] == CODE_REVIEW_SCHEMA

    with pytest.raises(Exception, match="immutable"), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE task_types SET approval_schema = '{}'::jsonb WHERE id = :id"),
            {"id": created["id"]},
        )


async def test_review_by_type_key_resolves_the_newest_active_version(
    client: httpx.AsyncClient,
) -> None:
    """The runner daemon names only the key: v2 without outcomes is superseded."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    assert (await _create_type(client, admin_key, "code-review")).status_code == 201
    newest = await _create_type(client, admin_key, "code-review", approvalSchema=CODE_REVIEW_SCHEMA)
    assert newest.status_code == 201

    review = await create_task(client, admin_key, title="Review", typeKey="code-review")
    assert (review["typeKey"], review["typeVersion"]) == ("code-review", 2)


# --- review fixes (TASK-000286) ---------------------------------------------------


@dataclass
class DenyingPolicy:
    """A PDP that refuses exactly the listed (action, resource) pairs."""

    deny: set[tuple[str, str]]
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    async def check(
        self, ctx, action, resource, *, contextual=(), on_behalf_of=None, consistency="default"
    ):  # type: ignore[no-untyped-def]
        self.calls.append((action, resource.key, on_behalf_of))
        allowed = (action, resource.key) not in self.deny
        return PolicyDecision(
            allowed=allowed,
            reason_code="allowed" if allowed else "denied_no_binding",
            decision_id=str(uuid.uuid4()),
            policy_version="1",
            model_version="1",
            source="online",
            consistency_token=None,
            evaluated_at=datetime.now(UTC),
            action=action,
            resource=resource.key,
        )

    async def list_objects(self, ctx, action, resource_type, **kwargs):  # type: ignore[no-untyped-def]
        return ObjectPage(objects=[], cursor=None, model_version="1")


@pytest.fixture
def restore_authorizer() -> Iterator[None]:
    yield
    configure_authorizer(Authorizer(None, "local"))


def _decide_as_iam_identity(sync_engine: Engine, approval_id: str, principal_id: str) -> str:
    """Make the recorded decision one taken with an IAM binding, not an API key.

    Deciding through a real IAM token needs the whole PEP; what the worker
    sees is only the snapshot, so the snapshot is what is arranged here.
    """
    iam_principal_id = str(uuid.uuid4())
    with sync_engine.begin() as conn:
        binding_id = conn.execute(
            text(
                "INSERT INTO iam_principal_bindings (id, tenant_id, principal_id, issuer, "
                "iam_tenant_id, iam_principal_id, permissions, status, created_at, updated_at) "
                "SELECT gen_random_uuid(), p.tenant_id, p.id, 'https://iam.test', p.tenant_id, "
                "CAST(:iam AS uuid), CAST(:perms AS jsonb), 'active', now(), now() "
                "FROM principals p WHERE p.id = :principal RETURNING id"
            ),
            {
                "iam": iam_principal_id,
                "perms": json.dumps(REVIEWER_PERMISSIONS),
                "principal": principal_id,
            },
        ).scalar_one()
        conn.execute(
            text(
                "UPDATE approvals SET decision_authority = decision_authority "
                "|| jsonb_build_object('credentialId', CAST(:binding AS text), "
                "'iamPrincipalId', CAST(:iam AS text)) WHERE id = :id"
            ),
            {"binding": str(binding_id), "iam": iam_principal_id, "id": approval_id},
        )
    return iam_principal_id


async def test_worker_authorizes_outcome_actions_with_the_configured_pdp(
    client: httpx.AsyncClient,
    settings: Settings,
    sync_engine: Engine,
    restore_authorizer: None,
) -> None:
    """CP_AUTHZ_MODE=policy: the worker asks the PDP, per resource, like the API.

    The decider's flat permissions include tasks.write, so the old worker (flat
    snapshot, no workspace) would have filed the fix task. The PDP says the
    decider may not write in the source task's workspace: the outcome fails.
    """
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "restricted")
    s = await _review_setup(
        client, admin_key=admin_key, coding_extra={"workspaceId": workspace["id"]}
    )
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "reject", "Needs work")
    iam_principal_id = _decide_as_iam_identity(sync_engine, approval_id, s["reviewer"]["id"])

    policy = DenyingPolicy(deny={("tasks.write", f"workspace:{workspace['id']}")})
    worker = Worker(settings, authorizer=Authorizer(policy, "policy"))
    try:
        await worker.run_once()
    finally:
        await worker.aclose()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "failed"
    failed = outcome["actions"][0]
    assert (failed["action"], failed["status"]) == ("ensureWork", "failed")
    assert failed["error"]["code"] == "forbidden"
    assert failed["error"]["details"]["resource"] == f"workspace:{workspace['id']}"
    assert (
        "tasks.write",
        f"workspace:{workspace['id']}",
        iam_principal_id,
    ) in policy.calls
    # What the outcome copies was read as the decider, through the PDP too.
    assert ("tasks.read", f"task:{s['coding']['id']}", iam_principal_id) in policy.calls
    assert len(_tasks_of_type(sync_engine, "coding-task")) == 1


async def test_outcome_waits_for_the_deciders_claim_instead_of_failing(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """The MCP scenario: claim the review, run it, decide the gate from the run.

    completeTask under that live claim used to fail with task_claimed and file
    a triage item on every reject. Now the outcome is deferred and finishes
    once the claim is gone.
    """
    s = await _review_setup(
        client,
        gate=False,
        reviewer_permissions=[*REVIEWER_PERMISSIONS, "tasks.claim", "sessions.open"],
    )
    review_id = s["review"]["id"]
    session = await open_session(client, s["reviewer_key"], client_name="human-mcp")
    claim = (await claim_task(client, s["reviewer_key"], review_id, session["id"])).json()
    run = await client.post(
        f"/api/v1/tasks/{review_id}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(s["reviewer_key"]),
    )
    assert run.status_code == 201, run.text
    approval = await _gate(client, s["admin_key"], review_id, s["reviewer"]["id"])
    approval_id = approval["id"]

    await _decide(client, s["reviewer_key"], approval_id, "reject", "Fix the parser")
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "deferred"
    assert [(a["action"], a["status"]) for a in outcome["actions"]] == [
        ("ensureWork", "executed"),  # the fix task does not wait for the claim
        ("completeTask", "not_executed"),
    ]
    assert len(_tasks_of_type(sync_engine, "coding-task")) == 2
    assert await _events(client, s["admin_key"], "approval.outcome_failed") == []

    # Re-checked while the claim is still live: still waiting, told once.
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE approvals SET outcome_next_attempt_at = now()"))
    await worker.run_once()
    assert (await _outcome(client, s["admin_key"], approval_id))["outcomeStatus"] == "deferred"
    assert len(await _events(client, s["admin_key"], "approval.outcome_deferred")) == 1

    # The human ends the run and lets the claim go; the outcome completes.
    failed_run = await client.post(
        f"/api/v1/runs/{run.json()['id']}:fail",
        json={"failureReason": "review finished"},
        headers=auth(s["reviewer_key"]),
    )
    assert failed_run.status_code == 200, failed_run.text
    released = await client.post(
        f"/api/v1/claims/{claim['id']}:release", json={}, headers=auth(s["reviewer_key"])
    )
    assert released.status_code == 200, released.text
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE approvals SET outcome_next_attempt_at = now()"))
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "executed"
    assert [a["attempts"] for a in outcome["actions"]] == [1, 1]
    review = await _task(client, s["admin_key"], review_id)
    assert review["systemStatusCategory"] == "terminal_success"
    assert len(_tasks_of_type(sync_engine, "coding-task")) == 2


def _break_ensure_work(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise KeyError("simulated bug outside the domain vocabulary")

    monkeypatch.setattr(outcomes_module, "_ensure_work", broken)


async def test_unexpected_errors_exhaust_into_a_failed_outcome_with_work(
    client: httpx.AsyncClient,
    settings: Settings,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = await _review_setup(client)
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "reject", "Needs work")
    _break_ensure_work(monkeypatch)
    worker = Worker(
        settings.model_copy(update={"outbox_max_attempts": 2, "outbox_backoff_base_seconds": 0})
    )
    try:
        await worker.run_once()
        # Still retrying — and the decision event was delivered regardless:
        # outbox delivery no longer waits for the outcome.
        outcome = await _outcome(client, s["admin_key"], approval_id)
        assert (outcome["outcomeStatus"], outcome["attempts"]) == ("pending", 1)
        assert "KeyError" in outcome["lastError"]
        with sync_engine.connect() as conn:
            delivered = conn.execute(
                text("SELECT delivered_at FROM outbox WHERE topic = 'approval.rejected'")
            ).scalar_one()
        assert delivered is not None

        await worker.run_once()
    finally:
        await worker.aclose()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "failed"
    first = outcome["actions"][0]
    assert (first["action"], first["status"]) == ("ensureWork", "failed")
    assert first["error"]["code"] == "outcome_attempts_exhausted"
    assert first["error"]["details"] == {"attempts": 2}
    assert "KeyError" in first["error"]["message"]
    failed = await _events(client, s["admin_key"], "approval.outcome_failed")
    assert len(failed) == 1
    triage = await _task(client, s["admin_key"], failed[0]["payload"]["failureWorkTaskId"])
    assert triage["assigneeId"] == s["reviewer"]["id"]
    assert len(_tasks_of_type(sync_engine, "coding-task")) == 1

    # Replayable once the cause is fixed.
    monkeypatch.undo()
    replayed = await client.post(
        f"/api/v1/approvals/{approval_id}:replay-outcome", json={}, headers=auth(s["reviewer_key"])
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["outcomeStatus"] == "executed"


async def test_a_stuck_pending_outcome_can_be_replayed(
    client: httpx.AsyncClient,
    settings: Settings,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = await _review_setup(client)
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "reject", "Needs work")

    # Fresh and untouched: the worker has it, replay is refused.
    early = await client.post(
        f"/api/v1/approvals/{approval_id}:replay-outcome", json={}, headers=auth(s["reviewer_key"])
    )
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "outcome_not_replayable"

    _break_ensure_work(monkeypatch)
    worker = Worker(settings)
    try:
        await worker.run_once()
    finally:
        await worker.aclose()
    monkeypatch.undo()
    assert (await _outcome(client, s["admin_key"], approval_id))["attempts"] == 1

    replayed = await client.post(
        f"/api/v1/approvals/{approval_id}:replay-outcome", json={}, headers=auth(s["reviewer_key"])
    )
    assert replayed.status_code == 200, replayed.text
    body = replayed.json()
    assert (body["outcomeStatus"], body["attempts"], body["lastError"]) == ("executed", 0, None)
    assert len(_tasks_of_type(sync_engine, "coding-task")) == 2


@pytest.mark.parametrize(
    ("missing", "expression"),
    [
        ("spawned", "$.spawnedBy.publicId!"),
        ("assigned", "$.spawnedBy.assigneeId!"),
        ("commit", "$.spawnedBy.artifact[commit].metadata.branch!"),
    ],
)
async def test_code_review_v3_refuses_to_file_fixes_without_owner_or_branch(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine, missing: str, expression: str
) -> None:
    """No source task, no coder, no branch: fail, never an unowned blank task."""
    s = await _review_setup(client, **{missing: False})
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "reject", "Needs work")
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "failed"
    error = outcome["actions"][0]["error"]
    assert error["code"] == "unresolved_expression"
    assert error["details"]["expression"] == expression
    assert outcome["actions"][1]["status"] == "not_executed"
    assert len(_tasks_of_type(sync_engine, "coding-task")) == 1


async def test_outcome_does_not_act_through_a_revoked_credential(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _review_setup(client)
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "reject", "Needs work")
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE api_keys SET revoked_at = now() WHERE principal_id = :p"),
            {"p": s["reviewer"]["id"]},
        )
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "failed"
    error = outcome["actions"][0]["error"]
    assert (error["code"], error["cause"]) == ("forbidden", "credential_inactive")
    assert len(_tasks_of_type(sync_engine, "coding-task")) == 1

    # An admin replaying on the decider's behalf keeps the (revoked) snapshot.
    replayed = await client.post(
        f"/api/v1/approvals/{approval_id}:replay-outcome", json={}, headers=auth(s["admin_key"])
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["outcomeStatus"] == "failed"
    assert replayed.json()["actions"][0]["error"]["cause"] == "credential_inactive"


async def test_outcome_reads_only_what_the_decider_may_read(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """code-review v3 copies the source's branch: no artifacts.read, no copy."""
    s = await _review_setup(
        client,
        reviewer_permissions=["approvals.read", "approvals.decide", "tasks.read", "tasks.write"],
    )
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "reject", "Needs work")
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "failed"
    error = outcome["actions"][0]["error"]
    assert error["code"] == "forbidden"
    assert error["details"]["required"] == ["artifacts.read"]
    assert len(_tasks_of_type(sync_engine, "coding-task")) == 1
