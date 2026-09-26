"""``invokeSkill`` in approval outcomes (CP-ADR-0061 step 2, TAI-ADR-0041).

The loop under test is the self-development one: a human approves a code
review and the review type — not core — declares that the reviewed branch is
merged by the ``git.merge@1`` skill. Core only queues a ``skill_invocation``
through the same path as ``POST /skills/{ref}:invoke``, with the decider's
authority and the decided approval as the basis of the external write, and
reacts to how the call ended with the actions the type declares.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.commands.approval_outcomes import (
    _basis_revoked,
    _decider_context,
    _Deferred,
    _finished_invocation,
)
from control_plane.config import Settings
from control_plane.infrastructure.db.models import Approval
from control_plane.worker.main import Worker
from tests.helpers import auth, create_agent_with_key, create_task, do_bootstrap
from tests.integration.test_approval_outcomes import (
    REVIEWER_PERMISSIONS,
    _decide,
    _events,
    _outcome,
    _review_setup,
    _task,
    _tasks_of_type,
)

# The contract of `git.merge@1` as `integrations/selfdev` publishes it
# (`git_merge.CONTRACT` there), minus the registry columns; the entrypoint
# is renamed, the executor only has to match it.
GIT_MERGE_CONTRACT: dict[str, Any] = {
    "idempotency": "natural",
    "timeoutSeconds": 300,
    "retryPolicy": {"maxAttempts": 3, "backoffSeconds": 30},
    "inputs": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["repository", "branch", "commit", "target"],
        "additionalProperties": False,
        "properties": {
            "repository": {"type": "string", "minLength": 1},
            "branch": {"type": "string", "minLength": 1},
            "commit": {"type": "string", "pattern": "^[0-9a-f]{7,40}$"},
            "target": {"type": "string", "minLength": 1},
            "message": {"type": "string"},
        },
    },
    "outputs": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["merged"],
        "properties": {
            "merged": {"type": "boolean"},
            "sha": {"type": "string"},
            "reason": {"enum": ["conflict", "branch_moved", "already_merged"]},
            "details": {"type": "string"},
        },
    },
    "implementation": {"protocol": "local", "entrypoint": "selfdev.git_merge:run"},
}
ENTRYPOINT = "selfdev.git_merge:run"

FIX_DESCRIPTION = (
    "Ревью $.task.publicId отклонено. Комментарий ревьюера:\n$.approval.comment\n\n"
    "Продолжи ветку $.task.customFields.branch!.\n\n"
    "Исходная задача $.spawnedBy.publicId:\n$.spawnedBy.description|truncate:4000"
)

# What `code-review` v4 is meant to look like (published by the superproject,
# not seeded by core): approve merges the reviewed commit and the review
# closes BY THE RESULT of the merge — a merge that did not happen files work
# for the coder and leaves the review open.
CODE_REVIEW_V4: dict[str, Any] = {
    "gates": {
        "default": {
            "outcomes": {
                "approved": [
                    {
                        "invokeSkill": {
                            "skill": "git.merge@1",
                            "inputs": {
                                "repository": "$.task.customFields.repository!",
                                "branch": "$.task.customFields.branch!",
                                "commit": "$.task.customFields.commit!",
                                "target": "$.task.customFields.targetBranch!",
                                "message": "Merge $.spawnedBy.publicId!: $.spawnedBy.title",
                            },
                            "expect": {"merged": True},
                            "onSuccess": [{"completeTask": {}}],
                            "onFailure": [
                                {
                                    "ensureWork": {
                                        "type": "coding-task",
                                        "key": "merge-fix:$.approval.id",
                                        "title": (
                                            "Влить $.spawnedBy.publicId! не удалось: "
                                            "$.invocation.output.reason"
                                            "$.invocation.error.code"
                                        ),
                                        "description": (
                                            "Ветка $.task.customFields.branch! не влита в "
                                            "$.task.customFields.targetBranch!.\n"
                                            "$.invocation.output.details|truncate:2000"
                                            "$.invocation.error.message"
                                        ),
                                        "assignee": "$.spawnedBy.assigneeId!",
                                        "priority": "$.spawnedBy.priority",
                                        "relation": {"spawned_by": "$.spawnedBy.id!"},
                                    }
                                }
                            ],
                        }
                    },
                ],
                "rejected": [
                    {
                        "ensureWork": {
                            "type": "coding-task",
                            "key": "review-fix:$.approval.id",
                            "title": "Правки по ревью $.spawnedBy.publicId!: $.spawnedBy.title",
                            "description": FIX_DESCRIPTION,
                            "assignee": "$.spawnedBy.assigneeId!",
                            "relation": {"spawned_by": "$.task.id!"},
                        }
                    },
                    {"completeTask": {}},
                ],
            }
        }
    }
}

MERGER_PERMISSIONS = [*REVIEWER_PERMISSIONS, "skills.invoke"]
REVIEW_FIELDS = {
    "repository": "https://forge.example/org/control-plane.git",
    "branch": "task/TASK-000001",
    "commit": "abc1234",
    "targetBranch": "main",
}


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


async def _patch_task(
    client: httpx.AsyncClient, key: str, task_id: str, body: dict[str, Any]
) -> None:
    task = await _task(client, key, task_id)
    response = await client.patch(
        f"/api/v1/tasks/{task_id}",
        json=body,
        headers={**auth(key), "If-Match": f'"task-{task["version"]}"'},
    )
    assert response.status_code == 200, response.text


async def _publish_git_merge(client: httpx.AsyncClient, key: str, version: str = "1") -> None:
    response = await client.post(
        "/api/v1/skills",
        json={
            "name": "git.merge",
            "version": version,
            "sideEffects": "external_write",
            "riskLevel": "medium",
            "contract": GIT_MERGE_CONTRACT,
        },
        headers=auth(key),
    )
    assert response.status_code == 201, response.text


async def _merge_setup(
    client: httpx.AsyncClient,
    *,
    permissions: list[str] | None = None,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A gated review whose custom fields say what to merge where."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    # Registered before the type: publication checks what an outcome invokes.
    await _publish_git_merge(client, admin_key)
    # A newer version must not be picked: the type pins `git.merge@1`.
    await _publish_git_merge(client, admin_key, version="2")
    s = await _review_setup(
        client,
        schema=CODE_REVIEW_V4,
        gate=False,
        reviewer_permissions=permissions or MERGER_PERMISSIONS,
        admin_key=admin_key,
    )
    await _patch_task(
        client,
        admin_key,
        s["review"]["id"],
        {"customFields": REVIEW_FIELDS if fields is None else fields},
    )
    approval = await client.post(
        "/api/v1/approvals",
        json={"task": s["review"]["id"], "assignedPrincipalId": s["reviewer"]["id"], "gate": True},
        headers=auth(admin_key),
    )
    assert approval.status_code == 201, approval.text
    _, executor_key = await create_agent_with_key(
        client, admin_key, name="executor", permissions=["skills.execute"]
    )
    return {**s, "approval": approval.json(), "executor_key": executor_key}


async def _claim(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/skill-invocations:claim",
        json={"protocols": ["local"], "localEntrypoints": [ENTRYPOINT]},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    lease: dict[str, Any] = response.json()["invocation"]
    return lease


def _make_outcomes_due(sync_engine: Engine) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE approvals SET outcome_next_attempt_at = now() "
                "WHERE outcome_status = 'deferred'"
            )
        )


async def test_approve_queues_git_merge_with_the_approval_as_basis(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _merge_setup(client)
    approval_id = s["approval"]["id"]

    await _decide(client, s["reviewer_key"], approval_id, "approve")
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    # Queued is done for the invokeSkill; the review closes only by the result
    # of the merge — closing it now would revoke the basis of the queued call.
    assert outcome["outcomeStatus"] == "deferred"
    assert [(a["action"], a["status"], a["reactsTo"], a["when"]) for a in outcome["actions"]] == [
        ("invokeSkill", "executed", None, None),
        ("completeTask", "not_executed", 0, "onSuccess"),
        ("ensureWork", "not_executed", 0, "onFailure"),
    ]
    queued = outcome["actions"][0]["result"]
    assert queued["skill"] == "git.merge@1"
    assert queued["created"] is True
    review = await _task(client, s["admin_key"], s["review"]["id"])
    assert review["systemStatusCategory"] not in ("terminal_success", "terminal_failure")

    invocation = (
        await client.get(
            f"/api/v1/skill-invocations/{queued['invocationId']}", headers=auth(s["reviewer_key"])
        )
    ).json()
    assert invocation["status"] == "pending"
    assert invocation["inputs"] == {
        "repository": REVIEW_FIELDS["repository"],
        "branch": REVIEW_FIELDS["branch"],
        "commit": REVIEW_FIELDS["commit"],
        "target": "main",
        "message": f"Merge {s['coding']['publicId']}: {s['coding']['title']}",
    }
    assert invocation["taskId"] == s["review"]["id"]
    assert invocation["authorizationBasis"] == {
        "kind": "approval",
        "approvalId": approval_id,
        "decidedBy": s["reviewer"]["id"],
    }
    with sync_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT i.idempotency_key, i.authority_principal_id, s.version "
                "FROM skill_invocations i JOIN skills s ON s.id = i.skill_id"
            )
        ).one()
    assert row.idempotency_key == f"approval-outcome:{approval_id}:0"
    assert str(row.authority_principal_id) == s["reviewer"]["id"]
    assert row.version == "1"

    deferred = await _events(client, s["admin_key"], "approval.outcome_deferred")
    assert [e["payload"]["reason"] for e in deferred] == ["skill_pending"]

    # Re-checks while the skill has not finished change nothing.
    _make_outcomes_due(sync_engine)
    await worker.run_once()
    assert (await _outcome(client, s["admin_key"], approval_id))["outcomeStatus"] == "deferred"

    # The claim re-checks the basis (M2.2): the review is open, the approval
    # stands — the call is handed out, not cancelled as task_terminal.
    lease = await _claim(client, s["executor_key"])
    assert lease["id"] == queued["invocationId"]
    done = await client.post(
        f"/api/v1/skill-invocations/{lease['id']}:complete",
        json={"fencingToken": lease["fencingToken"], "output": {"merged": True, "sha": "f" * 40}},
        headers=auth(s["executor_key"]),
    )
    assert done.status_code == 200, done.text

    _make_outcomes_due(sync_engine)
    await worker.run_once()
    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "executed"
    closed, not_needed = outcome["actions"][1], outcome["actions"][2]
    assert closed["status"] == "executed"
    assert closed["result"]["invocationId"] == queued["invocationId"]
    assert not_needed["status"] == "executed"
    assert not_needed["result"] == {"skipped": True, "invocationId": queued["invocationId"]}
    review = await _task(client, s["admin_key"], s["review"]["id"])
    assert review["systemStatusCategory"] == "terminal_success"
    assert len(_tasks_of_type(sync_engine, "coding-task")) == 1
    assert len(await _events(client, s["admin_key"], "approval.outcome_executed")) == 1

    # One approval, one merge: the basis is spent.
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM skill_invocations")).scalar() == 1


@pytest.mark.parametrize("ending", ["conflict", "failed"])
async def test_merge_that_did_not_happen_files_work_for_the_coder(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine, ending: str
) -> None:
    s = await _merge_setup(client)
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "approve")
    await worker.run_once()

    lease = await _claim(client, s["executor_key"])
    if ending == "conflict":
        # A contract answer, not an error: the skill succeeded, the merge did not.
        response = await client.post(
            f"/api/v1/skill-invocations/{lease['id']}:complete",
            json={
                "fencingToken": lease["fencingToken"],
                "output": {"merged": False, "reason": "conflict", "details": "CONFLICT in a.py"},
            },
            headers=auth(s["executor_key"]),
        )
    else:
        response = await client.post(
            f"/api/v1/skill-invocations/{lease['id']}:fail",
            json={
                "fencingToken": lease["fencingToken"],
                "error": {
                    "code": "push_rejected",
                    "message": "remote rejected the push",
                    "retryable": False,
                },
            },
            headers=auth(s["executor_key"]),
        )
    assert response.status_code == 200, response.text

    _make_outcomes_due(sync_engine)
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "executed"
    skipped, reaction = outcome["actions"][1], outcome["actions"][2]
    assert (skipped["action"], skipped["result"]) == (
        "completeTask",
        {"skipped": True, "invocationId": lease["id"]},
    )
    assert (reaction["action"], reaction["status"]) == ("ensureWork", "executed")
    assert reaction["result"]["invocationId"] == lease["id"]
    # Not merged: the review stays open.
    review = await _task(client, s["admin_key"], s["review"]["id"])
    assert review["systemStatusCategory"] not in ("terminal_success", "terminal_failure")

    coding_tasks = _tasks_of_type(sync_engine, "coding-task")
    assert len(coding_tasks) == 2
    fix = coding_tasks[1]
    source = s["coding"]
    assert str(fix.assignee_id) == s["coder"]["id"]
    assert fix.priority == "high"
    if ending == "conflict":
        assert fix.title == f"Влить {source['publicId']} не удалось: conflict"
        assert "CONFLICT in a.py" in fix.description
    else:
        assert fix.title == f"Влить {source['publicId']} не удалось: push_rejected"
        assert "remote rejected the push" in fix.description
    assert "Ветка task/TASK-000001 не влита в main." in fix.description
    relations = (
        await client.get(f"/api/v1/tasks/{fix.id}/relations", headers=auth(s["admin_key"]))
    ).json()["items"]
    assert [(r["type"], r["toTaskId"]) for r in relations if r["fromTaskId"] == str(fix.id)] == [
        ("spawned_by", source["id"])
    ]


async def test_invoke_skill_needs_the_deciders_right_to_invoke(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _merge_setup(client, permissions=REVIEWER_PERMISSIONS)
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "approve")
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "failed"
    first = outcome["actions"][0]
    assert (first["action"], first["status"]) == ("invokeSkill", "failed")
    assert first["error"]["code"] == "forbidden"
    assert first["error"]["details"]["required"] == ["skills.invoke"]
    # The review stays open for a replay: its approval is still a valid basis.
    assert outcome["actions"][1]["status"] == "not_executed"
    review = await _task(client, s["admin_key"], s["review"]["id"])
    assert review["systemStatusCategory"] != "terminal_success"
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM skill_invocations")).scalar() == 0


async def test_review_without_merge_fields_fails_instead_of_guessing(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    fields = {k: v for k, v in REVIEW_FIELDS.items() if k != "repository"}
    s = await _merge_setup(client, fields=fields)
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "approve")
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "failed"
    error = outcome["actions"][0]["error"]
    assert error["code"] == "unresolved_expression"
    assert error["details"]["expression"] == "$.task.customFields.repository!"
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM skill_invocations")).scalar() == 0


async def test_reject_files_fixes_with_a_bounded_copy_of_the_source(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """fix -> review -> fix chains must not grow: the source is truncated."""
    s = await _merge_setup(client)
    long_description = "y" * 6000
    await _patch_task(client, s["admin_key"], s["coding"]["id"], {"description": long_description})
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "reject", "Null check")
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "executed"
    fix = _tasks_of_type(sync_engine, "coding-task")[1]
    assert "Null check" in fix.description
    assert "y" * 3999 + "…" in fix.description
    assert "y" * 4000 not in fix.description
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM skill_invocations")).scalar() == 0


async def test_ensure_work_does_not_hand_out_a_task_the_decider_cannot_read(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """The ensure key is tenant-wide: finding a task by it is still a read."""
    schema = {
        "gates": {
            "default": {
                "outcomes": {
                    "rejected": [
                        {"ensureWork": {"type": "coding-task", "key": "shared", "title": "Fix"}}
                    ]
                }
            }
        }
    }
    s = await _review_setup(
        client,
        schema=schema,
        reviewer_permissions=["approvals.read", "approvals.decide", "tasks.write"],
    )
    hidden = await create_task(client, s["admin_key"], title="Somebody else's work")
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO external_references (id, tenant_id, entity_type, entity_id, "
                "external_system, external_type, external_id, metadata, version, created_by, "
                "created_at, updated_at) "
                "SELECT gen_random_uuid(), tenant_id, 'task', id, 'control-plane', "
                "'approval-outcome', 'shared', '{}'::jsonb, 1, created_by, now(), now() "
                "FROM tasks WHERE id = :id"
            ),
            {"id": hidden["id"]},
        )
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "reject", "No")
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "failed"
    error = outcome["actions"][0]["error"]
    assert error["code"] == "not_found"
    assert hidden["id"] not in str(error)


@pytest.mark.parametrize(
    ("approved", "fragment"),
    [
        ([{"completeTask": {}}, {"invokeSkill": {"skill": "git.merge@1"}}], "must come before"),
        ([{"transition": {"status": "done"}}, {"invokeSkill": {"skill": "git.merge@1"}}], "before"),
        # Closing next to the call would close the review before the call is
        # claimed: the claim would cancel it (basis_revoked / task_terminal).
        ([{"invokeSkill": {"skill": "git.merge@1"}}, {"completeTask": {}}], "may not have run"),
    ],
)
async def test_closing_the_task_next_to_invoke_skill_is_refused_at_publication(
    client: httpx.AsyncClient, approved: list[dict[str, Any]], fragment: str
) -> None:
    s = await _review_setup(client, gate=False)
    await _publish_git_merge(client, s["admin_key"])
    response = await client.post(
        "/api/v1/task-types",
        json={
            "key": "code-review",
            "displayName": "code-review",
            "approvalSchema": {"gates": {"default": {"outcomes": {"approved": approved}}}},
        },
        headers=auth(s["admin_key"]),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_approval_schema"
    assert fragment in response.json()["error"]["message"]


async def test_whole_v4_document_is_published_through_the_api(
    client: httpx.AsyncClient,
) -> None:
    """Core seeds no code-review version: the type comes in whole through the API."""
    s = await _review_setup(client, gate=False)
    await _publish_git_merge(client, s["admin_key"])
    response = await client.post(
        "/api/v1/task-types",
        json={"key": "code-review", "displayName": "Code review", "approvalSchema": CODE_REVIEW_V4},
        headers=auth(s["admin_key"]),
    )
    assert response.status_code == 201, response.text
    assert response.json()["approvalSchema"] == CODE_REVIEW_V4


@pytest.mark.parametrize(
    ("outcomes", "reason"),
    [
        # An external write through whatever version is newest when decided.
        ({"approved": [{"invokeSkill": {"skill": "git.merge"}}]}, "external_write_not_pinned"),
        # A rejection is not a basis for an external write (ADR-0056 §4).
        ({"rejected": [{"invokeSkill": {"skill": "git.merge@1"}}]}, "external_write_on_rejected"),
        ({"approved": [{"invokeSkill": {"skill": "git.merge@7"}}]}, "not_found"),
        ({"approved": [{"invokeSkill": {"skill": "nothing"}}]}, "not_found"),
    ],
)
async def test_what_an_outcome_may_invoke_is_checked_against_the_registry(
    client: httpx.AsyncClient, outcomes: dict[str, Any], reason: str
) -> None:
    s = await _review_setup(client, gate=False)
    await _publish_git_merge(client, s["admin_key"])
    response = await client.post(
        "/api/v1/task-types",
        json={
            "key": "code-review",
            "displayName": "code-review",
            "approvalSchema": {"gates": {"default": {"outcomes": outcomes}}},
        },
        headers=auth(s["admin_key"]),
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert (error["code"], error["details"]["reason"]) == ("invalid_approval_schema", reason)


async def test_an_unpinned_reference_that_became_an_external_write_is_refused_when_run(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """Published against a harmless skill; by the decision, its newest version writes."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    harmless = await client.post(
        "/api/v1/skills",
        json={
            "name": "git.merge",
            "version": "0",
            "sideEffects": "none",
            "riskLevel": "low",
            "contract": GIT_MERGE_CONTRACT,
        },
        headers=auth(admin_key),
    )
    assert harmless.status_code == 201, harmless.text
    schema = {
        "gates": {
            "default": {
                "outcomes": {
                    "approved": [
                        {
                            "invokeSkill": {
                                "skill": "git.merge",
                                "inputs": {"branch": "$.task.customFields.branch!"},
                            }
                        }
                    ]
                }
            }
        }
    }
    s = await _review_setup(
        client,
        schema=schema,
        reviewer_permissions=MERGER_PERMISSIONS,
        admin_key=admin_key,
    )
    await _patch_task(client, admin_key, s["review"]["id"], {"customFields": REVIEW_FIELDS})
    await _publish_git_merge(client, admin_key)
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "approve")
    await worker.run_once()

    outcome = await _outcome(client, admin_key, approval_id)
    assert outcome["outcomeStatus"] == "failed"
    error = outcome["actions"][0]["error"]
    assert (error["code"], error["details"]["input"]) == ("invalid_action_input", "skill")
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM skill_invocations")).scalar() == 0


async def test_a_call_nobody_claims_is_cancelled_and_reacted_to_as_a_failure(
    client: httpx.AsyncClient, settings: Settings, sync_engine: Engine
) -> None:
    """No executor for skill_wait: the outcome does not stay deferred forever."""
    s = await _merge_setup(client)
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "approve")
    impatient = Worker(settings.model_copy(update={"approval_outcome_skill_wait_seconds": 0.0}))
    try:
        await impatient.run_once()
    finally:
        await impatient.engine.dispose()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "executed"
    assert [(a["action"], a["status"]) for a in outcome["actions"]] == [
        ("invokeSkill", "executed"),
        ("completeTask", "executed"),
        ("ensureWork", "executed"),
    ]
    assert outcome["actions"][1]["result"]["skipped"] is True
    invocation_id = outcome["actions"][0]["result"]["invocationId"]
    invocation = (
        await client.get(
            f"/api/v1/skill-invocations/{invocation_id}", headers=auth(s["reviewer_key"])
        )
    ).json()
    assert invocation["status"] == "cancelled"
    # The basis stands (the review is open): an expired wait, not a revoked basis.
    assert (invocation["error"]["code"], invocation["error"]["message"]) == (
        "cancelled",
        "approval_outcome_skill_wait_expired",
    )
    # Core stopped waiting; the decider did not ask for it.
    assert invocation["error"]["details"]["initiator"] == "system"
    fix = _tasks_of_type(sync_engine, "coding-task")[1]
    assert fix.title == f"Влить {s['coding']['publicId']} не удалось: cancelled"
    review = await _task(client, s["admin_key"], s["review"]["id"])
    assert review["systemStatusCategory"] not in ("terminal_success", "terminal_failure")


async def test_review_closed_by_hand_while_the_merge_is_queued_files_no_work(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """The basis is revoked before anybody tried the merge: no reaction runs.

    approve -> git.merge queued -> a human closes the review by hand -> the
    claim cancels the call (basis_revoked / task_terminal, the M2.1
    guarantee) -> the outcome ends ``executed`` with both reactions skipped,
    and no work "merging failed" is filed for a merge nobody attempted.
    """
    s = await _merge_setup(client)
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "approve")
    await worker.run_once()
    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "deferred"
    invocation_id = outcome["actions"][0]["result"]["invocationId"]

    review = await _task(client, s["admin_key"], s["review"]["id"])
    closed = await client.post(
        f"/api/v1/tasks/{review['id']}:complete",
        headers={**auth(s["admin_key"]), "If-Match": f'"task-{review["version"]}"'},
    )
    assert closed.status_code == 200, closed.text

    claimed = await client.post(
        "/api/v1/skill-invocations:claim",
        json={"protocols": ["local"], "localEntrypoints": [ENTRYPOINT]},
        headers=auth(s["executor_key"]),
    )
    # Nothing to hand out: the only call was cancelled at claim.
    assert claimed.status_code == 204, claimed.text
    invocation = (
        await client.get(
            f"/api/v1/skill-invocations/{invocation_id}", headers=auth(s["reviewer_key"])
        )
    ).json()
    assert invocation["status"] == "cancelled"
    assert (invocation["error"]["code"], invocation["error"]["message"]) == (
        "basis_revoked",
        "task_terminal",
    )

    _make_outcomes_due(sync_engine)
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "executed"
    skipped = {
        "skipped": True,
        "reason": "basis_revoked",
        "cause": "task_terminal",
        "invocationId": invocation_id,
    }
    assert [(a["action"], a["status"], a["result"]) for a in outcome["actions"][1:]] == [
        ("completeTask", "executed", skipped),
        ("ensureWork", "executed", skipped),
    ]
    assert len(await _events(client, s["admin_key"], "approval.outcome_executed")) == 1
    assert await _events(client, s["admin_key"], "approval.outcome_failed") == []
    # No work about a merge that nobody attempted; the review stays as the human left it.
    assert len(_tasks_of_type(sync_engine, "coding-task")) == 1
    review = await _task(client, s["admin_key"], s["review"]["id"])
    assert review["systemStatusCategory"] == "terminal_success"


async def _close_review_by_hand(client: httpx.AsyncClient, s: dict[str, Any]) -> None:
    review = await _task(client, s["admin_key"], s["review"]["id"])
    closed = await client.post(
        f"/api/v1/tasks/{review['id']}:complete",
        headers={**auth(s["admin_key"]), "If-Match": f'"task-{review["version"]}"'},
    )
    assert closed.status_code == 200, closed.text


async def _assert_revoked_without_work(
    client: httpx.AsyncClient,
    sync_engine: Engine,
    s: dict[str, Any],
    invocation_id: str,
    cause: str = "task_terminal",
    by_outcome: bool = True,
) -> None:
    """The call is cancelled as basis_revoked and the outcome ends with no reaction."""
    invocation = (
        await client.get(
            f"/api/v1/skill-invocations/{invocation_id}", headers=auth(s["reviewer_key"])
        )
    ).json()
    assert invocation["status"] == "cancelled"
    assert (invocation["error"]["code"], invocation["error"]["message"]) == (
        "basis_revoked",
        cause,
    )
    # Revoked by core on a lost basis: by the outcome pass under the decider's
    # authority, or by a claim under the executor's.
    assert invocation["error"]["details"]["initiator"] == "system"
    if by_outcome:
        assert invocation["error"]["details"]["cancelledBy"] == s["reviewer"]["id"]
    outcome = await _outcome(client, s["admin_key"], s["approval"]["id"])
    assert outcome["outcomeStatus"] == "executed"
    skipped = {
        "skipped": True,
        "reason": "basis_revoked",
        "cause": cause,
        "invocationId": invocation_id,
    }
    assert [(a["action"], a["status"], a["result"]) for a in outcome["actions"][1:]] == [
        ("completeTask", "executed", skipped),
        ("ensureWork", "executed", skipped),
    ]
    assert len(await _events(client, s["admin_key"], "approval.outcome_executed")) == 1
    assert await _events(client, s["admin_key"], "approval.outcome_failed") == []
    assert len(_tasks_of_type(sync_engine, "coding-task")) == 1


async def _assert_review_closed(client: httpx.AsyncClient, s: dict[str, Any]) -> None:
    review = await _task(client, s["admin_key"], s["review"]["id"])
    assert review["systemStatusCategory"] == "terminal_success"


async def test_expired_wait_on_a_review_closed_by_hand_files_no_work(
    client: httpx.AsyncClient, worker: Worker, settings: Settings, sync_engine: Engine
) -> None:
    """No executor, the review closed by hand, the wait is over: the basis wins.

    The expiry is not reported as a failed merge — the call is cancelled as
    ``basis_revoked`` and neither reaction runs, exactly as a claim would.
    """
    s = await _merge_setup(client)
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "approve")
    await worker.run_once()
    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "deferred"
    invocation_id = outcome["actions"][0]["result"]["invocationId"]

    await _close_review_by_hand(client, s)
    _make_outcomes_due(sync_engine)
    impatient = Worker(settings.model_copy(update={"approval_outcome_skill_wait_seconds": 0.0}))
    try:
        await impatient.run_once()
    finally:
        await impatient.engine.dispose()

    await _assert_revoked_without_work(client, sync_engine, s, invocation_id)
    await _assert_review_closed(client, s)


async def test_review_closed_by_hand_revokes_the_queued_merge_on_the_next_pass(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """The outcome does not sit deferred for skill_wait with its basis gone.

    Nobody claims the call and its wait is far from over: the next pass of
    the outcome finds the review closed and cancels the call itself.
    """
    s = await _merge_setup(client)
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "approve")
    await worker.run_once()
    invocation_id = (await _outcome(client, s["admin_key"], approval_id))["actions"][0]["result"][
        "invocationId"
    ]

    await _close_review_by_hand(client, s)
    _make_outcomes_due(sync_engine)
    await worker.run_once()

    await _assert_revoked_without_work(client, sync_engine, s, invocation_id)
    await _assert_review_closed(client, s)
    with sync_engine.connect() as conn:
        cancelled = conn.execute(
            text(
                "SELECT payload->>'code', payload->>'reason', payload->>'initiator', "
                "payload->>'cancelledBy' FROM events "
                "WHERE event_type = 'skill.invocation_cancelled'"
            )
        ).all()
    assert [tuple(row) for row in cancelled] == [
        ("basis_revoked", "task_terminal", "system", s["reviewer"]["id"])
    ]


async def _queued_merge(client: httpx.AsyncClient, worker: Worker) -> tuple[dict[str, Any], str]:
    """An approved review whose merge is queued and nobody has claimed yet."""
    s = await _merge_setup(client)
    approval_id = s["approval"]["id"]
    await _decide(client, s["reviewer_key"], approval_id, "approve")
    await worker.run_once()
    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "deferred"
    return s, outcome["actions"][0]["result"]["invocationId"]


async def _disable_git_merge_1(
    client: httpx.AsyncClient, s: dict[str, Any], sync_engine: Engine
) -> None:
    with sync_engine.connect() as conn:
        skill_id = conn.execute(
            text("SELECT id FROM skills WHERE name = 'git.merge' AND version = '1'")
        ).scalar_one()
    disabled = await client.patch(
        f"/api/v1/skills/{skill_id}",
        json={"status": "disabled"},
        headers={**auth(s["admin_key"]), "If-Match": '"skill-1"'},
    )
    assert disabled.status_code == 200, disabled.text


@pytest.mark.parametrize("claimed_first", [False, True])
async def test_disabled_merge_on_a_review_closed_by_hand_files_no_work(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine, claimed_first: bool
) -> None:
    """The basis is named before the version: a disabled skill hides nothing.

    ``git.merge@1`` is disabled and the review closed by hand. Whoever gets to
    the call first — the outcome pass or an executor's claim — cancels it as
    ``basis_revoked`` for ``task_terminal``, not ``skill_disabled``, and no
    "merging failed" is filed for it.
    """
    s, invocation_id = await _queued_merge(client, worker)
    await _disable_git_merge_1(client, s, sync_engine)
    await _close_review_by_hand(client, s)
    if claimed_first:
        claimed = await client.post(
            "/api/v1/skill-invocations:claim",
            json={"protocols": ["local"], "localEntrypoints": [ENTRYPOINT]},
            headers=auth(s["executor_key"]),
        )
        assert claimed.status_code == 204, claimed.text
    _make_outcomes_due(sync_engine)
    await worker.run_once()

    await _assert_revoked_without_work(
        client, sync_engine, s, invocation_id, by_outcome=not claimed_first
    )
    await _assert_review_closed(client, s)


async def test_withdrawn_approval_revokes_the_queued_merge(
    client: httpx.AsyncClient, app: FastAPI, worker: Worker, sync_engine: Engine
) -> None:
    """``approval_withdrawn``: the approval behind the call is no longer approved.

    No API takes a decided approval back, so the test withdraws it in the
    database. The executor of the outcome keys it off the approval's status,
    so the pass is driven through ``_finished_invocation`` while the approval
    is withdrawn; the status is then put back only so that the executor can
    reach the reactions and show that both are skipped for this cause.
    """
    s, invocation_id = await _queued_merge(client, worker)
    approval_id = uuid.UUID(s["approval"]["id"])
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE approvals SET status = 'cancelled' WHERE id = :id"), {"id": approval_id}
        )

    async with app.state.session_factory() as session:
        approval = await session.get(Approval, approval_id)
        assert approval is not None
        ctx = _decider_context(approval, trace_run_id="", causation_id=None)
        # The wait is far from over: only the lost basis cancels the call.
        called, _ = await _finished_invocation(
            session, ctx, uuid.UUID(invocation_id), timedelta(hours=1)
        )
        assert _basis_revoked(called) == "approval_withdrawn"
        await session.commit()

    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE approvals SET status = 'approved' WHERE id = :id"), {"id": approval_id}
        )
    _make_outcomes_due(sync_engine)
    await worker.run_once()

    await _assert_revoked_without_work(
        client, sync_engine, s, invocation_id, cause="approval_withdrawn"
    )
    # Nothing closed the review: it stays as it was.
    review = await _task(client, s["admin_key"], s["review"]["id"])
    assert review["systemStatusCategory"] not in ("terminal_success", "terminal_failure")


async def test_a_pass_with_nothing_to_do_leaves_the_call_free_to_claim(
    client: httpx.AsyncClient, app: FastAPI, worker: Worker
) -> None:
    """The row of a pending call is locked only when the pass will cancel it.

    With the basis standing and the wait not over, a pass of the outcome
    holds no lock: a claim in a parallel transaction (``SKIP LOCKED``) takes
    the call while the pass's transaction is still open.
    """
    s, invocation_id = await _queued_merge(client, worker)

    async with app.state.session_factory() as session:
        approval = await session.get(Approval, uuid.UUID(s["approval"]["id"]))
        assert approval is not None
        ctx = _decider_context(approval, trace_run_id="", causation_id=None)
        with pytest.raises(_Deferred):
            await _finished_invocation(session, ctx, uuid.UUID(invocation_id), timedelta(hours=1))
        assert session.in_transaction()

        lease = await _claim(client, s["executor_key"])
        assert lease["id"] == invocation_id
        await session.rollback()
