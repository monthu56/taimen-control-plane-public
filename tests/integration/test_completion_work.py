"""ensureWork.customFields and work after completion (CP-ADR-0061, amendment 2026-09-25).

Two halves of one chain. An approval outcome's ``ensureWork`` hands the next
step machine-readable fields (``customFields``) instead of prose; a task type
declares what core files once a task of it is completed (``completionSchema``)
— the review of a finished coding task, with its gate — so whoever completes
the task (a runner, a human's harness, an approval outcome) gets the chain,
and no runner daemon has to know about it.
"""

import copy
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from control_plane.worker.main import Worker
from tests.helpers import (
    auth,
    claim_task,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    open_session,
)
from tests.integration.test_approval_outcomes import (
    _create_type,
    _decide,
    _outcome,
    _review_setup,
    _task,
    _tasks_of_type,
    worker,
)

__all__ = ["worker"]

# --- ensureWork.customFields in an approval outcome ---------------------------


def _fix_schema(custom_fields: dict[str, str], *, twice: bool = False) -> dict[str, Any]:
    ensure = {
        "ensureWork": {
            "type": "coding-task",
            "key": "fix:$.approval.id",
            "title": "Fix $.spawnedBy.publicId",
            "relation": {"spawned_by": "$.task.id"},
            "customFields": custom_fields,
        }
    }
    again = {
        "ensureWork": {
            "type": "coding-task",
            "key": "fix:$.approval.id",
            "title": "Another title",
            "customFields": {"branch": "somewhere-else"},
        }
    }
    rejected = [ensure, *([again] if twice else []), {"completeTask": {}}]
    return {"gates": {"default": {"outcomes": {"rejected": rejected}}}}


FIELDS = {
    "branch": "$.spawnedBy.artifact[commit].metadata.branch!",
    "notes": "$.approval.comment|truncate:10",
    "source": "$.spawnedBy.publicId",
    "empty": "$.spawnedBy.customFields.nothing",
}
CODING_FIELDS = {
    "type": "object",
    "properties": {
        "branch": {"type": "string"},
        "notes": {"type": "string"},
        "source": {"type": "string"},
    },
    "required": ["branch"],
    "additionalProperties": False,
}


async def test_custom_fields_reach_the_filed_work_and_the_ensure_keeps_them(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _review_setup(client, schema=_fix_schema(FIELDS, twice=True))
    # The next version of the target type is what ensureWork files under,
    # and its field_schema is what the rendered fields are checked against.
    published = await _create_type(client, s["admin_key"], "coding-task", fieldSchema=CODING_FIELDS)
    assert published.status_code == 201, published.text
    approval_id = s["approval"]["id"]

    await _decide(client, s["reviewer_key"], approval_id, "reject", "Null check is missing")
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "executed", outcome
    first, second = outcome["actions"][0]["result"], outcome["actions"][1]["result"]
    assert first["created"] is True
    assert first["customFields"] == ["branch", "notes", "source"]
    # Same key: the work item is found, not rewritten.
    assert second == {"taskId": first["taskId"], "publicId": first["publicId"], "created": False}

    coding_tasks = _tasks_of_type(sync_engine, "coding-task")
    assert len(coding_tasks) == 2
    fix = await _task(client, s["admin_key"], first["taskId"])
    source = s["coding"]["publicId"]
    # A value that resolved to nothing is left out, not filed blank.
    assert fix["customFields"] == {
        "branch": f"task/{source}",
        "notes": "Null chec…",
        "source": source,
    }
    assert fix["title"] == f"Fix {source}"


async def test_fields_the_target_type_refuses_fail_the_outcome_and_lose_nothing(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _review_setup(client, schema=_fix_schema({"branch": "$.spawnedBy.publicId"}))
    strict = {**CODING_FIELDS, "properties": {"branch": {"type": "string", "pattern": "^task/"}}}
    assert (
        await _create_type(client, s["admin_key"], "coding-task", fieldSchema=strict)
    ).status_code == 201
    approval_id = s["approval"]["id"]

    await _decide(client, s["reviewer_key"], approval_id, "reject", "Redo")
    await worker.run_once()

    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "failed"
    assert [(a["action"], a["status"]) for a in outcome["actions"]] == [
        ("ensureWork", "failed"),
        ("completeTask", "not_executed"),
    ]
    assert outcome["actions"][0]["error"]["code"] == "custom_fields_invalid"
    review = await _task(client, s["admin_key"], s["review"]["id"])
    assert review["systemStatusCategory"] != "terminal_success"

    # The type is fixed with a new version; the replay resumes and finishes.
    assert (
        await _create_type(client, s["admin_key"], "coding-task", fieldSchema=CODING_FIELDS)
    ).status_code == 201
    replayed = await client.post(
        f"/api/v1/approvals/{approval_id}:replay-outcome", json={}, headers=auth(s["reviewer_key"])
    )
    assert replayed.status_code == 200, replayed.text
    outcome = await _outcome(client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "executed"
    fix = await _task(client, s["admin_key"], outcome["actions"][0]["result"]["taskId"])
    assert fix["customFields"] == {"branch": s["coding"]["publicId"]}


@pytest.mark.parametrize(
    "ensure",
    [
        {"customFields": {"bad-name": "x"}},
        {"customFields": {"branch": 5}},
        {"customFields": {}},
        {"customFields": {"branch": "$.nowhere.x"}},
        {"requestApproval": {}},
        {"requestApproval": {"assignee": "x", "extra": "y"}},
    ],
)
async def test_malformed_custom_fields_and_approvals_are_refused_at_publication(
    client: httpx.AsyncClient, ensure: dict[str, Any]
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    action = {"ensureWork": {"type": "coding-task", "key": "k", "title": "t", **ensure}}
    response = await _create_type(
        client,
        admin_key,
        "code-review",
        approvalSchema={"gates": {"default": {"outcomes": {"rejected": [action]}}}},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_approval_schema"


# --- work after completion ----------------------------------------------------

RUNNER_PERMISSIONS = [
    "sessions.open",
    "tasks.read",
    "tasks.write",
    "tasks.claim",
    "artifacts.read",
    "approvals.manage",
]
REVIEW_FIELDS = {
    "type": "object",
    "properties": {"branch": {"type": "string"}, "commit": {"type": "string"}},
    "required": ["branch", "commit"],
    "additionalProperties": False,
}


def _reopenable() -> dict[str, Any]:
    lifecycle = copy.deepcopy(SYSTEM_TASK_LIFECYCLE)
    lifecycle["transitions"].append({"from": "done", "to": ["todo"]})
    return lifecycle


def _completion_schema(reviewer_id: str) -> dict[str, Any]:
    """What the runner's auto-review does today, declared by the type."""
    return {
        "onComplete": {
            "when": [
                "$.task.artifact[commit].metadata.published",
                "$.task.artifact[commit].metadata.branch",
            ],
            "actions": [
                {
                    "ensureWork": {
                        "type": "code-review",
                        "key": "code-review:$.task.id",
                        "title": "Review $.task.publicId: $.task.title",
                        "description": "Branch $.task.artifact[commit].metadata.branch",
                        "assignee": reviewer_id,
                        "relation": {"spawned_by": "$.task.id"},
                        "customFields": {
                            "branch": "$.task.artifact[commit].metadata.branch!",
                            "commit": "$.task.artifact[commit].metadata.commit!",
                        },
                        "requestApproval": {
                            "assignee": reviewer_id,
                            "comment": "Code review $.task.publicId",
                        },
                    }
                },
                {"comment": {"body": "Review filed for $.task.publicId"}},
            ],
        }
    }


async def _completion_setup(
    client: httpx.AsyncClient,
    *,
    runner_permissions: list[str] | None = None,
    declared: bool = True,
) -> dict[str, Any]:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    reviewer, _ = await create_agent_with_key(client, admin_key, name="reviewer")
    runner, runner_key = await create_agent_with_key(
        client, admin_key, name="runner", permissions=runner_permissions or RUNNER_PERMISSIONS
    )
    review_type = await _create_type(client, admin_key, "code-review", fieldSchema=REVIEW_FIELDS)
    assert review_type.status_code == 201, review_type.text
    extra = {"completionSchema": _completion_schema(reviewer["id"])} if declared else {}
    coding_type = await _create_type(
        client, admin_key, "coding-task", lifecycleSchema=_reopenable(), **extra
    )
    assert coding_type.status_code == 201, coding_type.text
    coding = await create_task(client, admin_key, title="Implement it", typeKey="coding-task")
    return {
        "admin_key": admin_key,
        "reviewer": reviewer,
        "runner": runner,
        "runner_key": runner_key,
        "coding": coding,
        "coding_type": coding_type.json(),
    }


async def _commit(
    client: httpx.AsyncClient, key: str, task_id: str, *, published: bool = True
) -> None:
    response = await client.post(
        "/api/v1/artifacts",
        json={
            "type": "commit",
            "name": "commit",
            "task": task_id,
            "metadata": {"branch": "task/X", "commit": "abc123", "published": published},
        },
        headers=auth(key),
    )
    assert response.status_code == 201, response.text


async def _run_to_success(client: httpx.AsyncClient, key: str, task_id: str) -> None:
    """The runner's cycle: claim, run, succeed (which completes the task)."""
    session = await open_session(client, key, client_name="runner")
    claim = await claim_task(client, key, task_id, session["id"])
    assert claim.status_code in (200, 201), claim.text
    run = await client.post(
        f"/api/v1/tasks/{task_id}:start-run",
        json={"claimId": claim.json()["id"], "fencingToken": claim.json()["fencingToken"]},
        headers=auth(key),
    )
    assert run.status_code == 201, run.text
    done = await client.post(f"/api/v1/runs/{run.json()['id']}:succeed", json={}, headers=auth(key))
    assert done.status_code == 200, done.text


async def _complete(client: httpx.AsyncClient, key: str, task_id: str) -> httpx.Response:
    task = await _task(client, key, task_id)
    return await client.post(
        f"/api/v1/tasks/{task_id}:complete",
        json={},
        headers={**auth(key), "If-Match": f'"task-{task["version"]}"'},
    )


async def _task_events(client: httpx.AsyncClient, key: str, event_type: str) -> list[Any]:
    items = (
        await client.get("/api/v1/events?limit=200&entityType=task", headers=auth(key))
    ).json()["items"]
    return [e for e in items if e["type"] == event_type]


def _completion_rows(sync_engine: Engine) -> list[Any]:
    with sync_engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT task_id, status, attempts, actions, error, completed_by "
                    "FROM task_completion_work"
                )
            ).all()
        )


async def test_completed_run_files_the_declared_review_with_its_gate(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _completion_setup(client)
    coding_id = s["coding"]["id"]
    await _commit(client, s["admin_key"], coding_id)

    await _run_to_success(client, s["runner_key"], coding_id)

    coding = await _task(client, s["admin_key"], coding_id)
    assert coding["systemStatusCategory"] == "terminal_success"
    (review_row,) = _tasks_of_type(sync_engine, "code-review")
    review = await _task(client, s["admin_key"], str(review_row.id))
    assert review["title"] == f"Review {coding['publicId']}: Implement it"
    assert review["assigneeId"] == s["reviewer"]["id"]
    assert review["customFields"] == {"branch": "task/X", "commit": "abc123"}
    # Filed as a step of the completion, by the principal who completed it.
    assert review_row.origin == {
        "kind": "process",
        "ref": f"completion:{coding_id}",
        "evidence": [],
    }
    assert review["createdBy"] == s["runner"]["id"]

    relations = (
        await client.get(f"/api/v1/tasks/{review['id']}/relations", headers=auth(s["admin_key"]))
    ).json()["items"]
    assert [(r["type"], r["toTaskId"]) for r in relations if r["fromTaskId"] == review["id"]] == [
        ("spawned_by", coding_id)
    ]
    approvals = (
        await client.get(f"/api/v1/approvals?taskId={review['id']}", headers=auth(s["admin_key"]))
    ).json()["items"]
    assert [(a["gate"], a["status"], a["assignedPrincipalId"]) for a in approvals] == [
        (True, "pending", s["reviewer"]["id"])
    ]
    assert approvals[0]["comment"] == f"Code review {coding['publicId']}"

    comments = (
        await client.get(f"/api/v1/tasks/{coding_id}/comments", headers=auth(s["admin_key"]))
    ).json()["items"]
    assert [c["body"] for c in comments] == [f"Review filed for {coding['publicId']}"]

    executed = await _task_events(client, s["admin_key"], "task.completion_work_executed")
    assert len(executed) == 1
    assert executed[0]["actorId"] == s["runner"]["id"]
    assert [a["action"] for a in executed[0]["payload"]["actions"]] == ["ensureWork", "comment"]
    ((task_id, status, attempts, _, error, completed_by),) = _completion_rows(sync_engine)
    assert (str(task_id), status, attempts, error) == (coding_id, "executed", 1, None)
    assert str(completed_by) == s["runner"]["id"]


async def test_a_repeated_completion_files_nothing_twice(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _completion_setup(client)
    coding_id = s["coding"]["id"]
    await _commit(client, s["admin_key"], coding_id)
    await _run_to_success(client, s["runner_key"], coding_id)

    # Reopened and completed again, this time by a human.
    task = await _task(client, s["admin_key"], coding_id)
    reopened = await client.patch(
        f"/api/v1/tasks/{coding_id}",
        json={"status": "todo"},
        headers={**auth(s["admin_key"]), "If-Match": f'"task-{task["version"]}"'},
    )
    assert reopened.status_code == 200, reopened.text
    completed = await _complete(client, s["admin_key"], coding_id)
    assert completed.status_code == 200, completed.text

    assert len(_tasks_of_type(sync_engine, "code-review")) == 1
    assert len(await _task_events(client, s["admin_key"], "task.completion_work_executed")) == 1
    ((_, status, attempts, _, _, _),) = _completion_rows(sync_engine)
    assert (status, attempts) == ("executed", 1)


@pytest.mark.parametrize("artifact", ["none", "unpublished"])
async def test_without_a_published_commit_nothing_is_filed(
    client: httpx.AsyncClient, sync_engine: Engine, artifact: str
) -> None:
    s = await _completion_setup(client)
    coding_id = s["coding"]["id"]
    if artifact == "unpublished":
        await _commit(client, s["admin_key"], coding_id, published=False)

    completed = await _complete(client, s["admin_key"], coding_id)

    assert completed.status_code == 200, completed.text
    assert completed.json()["systemStatusCategory"] == "terminal_success"
    assert _tasks_of_type(sync_engine, "code-review") == []
    assert _completion_rows(sync_engine) == []
    assert await _task_events(client, s["admin_key"], "task.completion_work_executed") == []


async def test_a_refused_action_is_recorded_and_the_completion_stands(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Without approvals.manage the gate cannot be requested: the review filed in
    the same action is rolled back with it, the run still succeeds."""
    s = await _completion_setup(
        client, runner_permissions=[p for p in RUNNER_PERMISSIONS if p != "approvals.manage"]
    )
    coding_id = s["coding"]["id"]
    await _commit(client, s["admin_key"], coding_id)

    await _run_to_success(client, s["runner_key"], coding_id)

    coding = await _task(client, s["admin_key"], coding_id)
    assert coding["systemStatusCategory"] == "terminal_success"
    assert _tasks_of_type(sync_engine, "code-review") == []
    ((_, status, _, actions, error, _),) = _completion_rows(sync_engine)
    assert status == "failed"
    assert error["code"] == "forbidden"
    assert [(a["action"], a["status"]) for a in actions] == [("ensureWork", "failed")]
    failed = await _task_events(client, s["admin_key"], "task.completion_work_failed")
    assert len(failed) == 1
    assert failed[0]["payload"]["failedAction"]["index"] == 0
    comments = (
        await client.get(f"/api/v1/tasks/{coding_id}/comments", headers=auth(s["admin_key"]))
    ).json()["items"]
    assert len(comments) == 1
    assert "forbidden" in comments[0]["body"]
    assert comments[0]["authorPrincipalId"] != s["runner"]["id"]


async def test_type_without_completion_schema_files_nothing(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _completion_setup(client, declared=False)
    assert s["coding_type"]["completionSchema"] == {}
    await _commit(client, s["admin_key"], s["coding"]["id"])

    await _run_to_success(client, s["runner_key"], s["coding"]["id"])

    assert _tasks_of_type(sync_engine, "code-review") == []
    assert _completion_rows(sync_engine) == []


async def test_completion_schema_is_part_of_the_published_version(
    client: httpx.AsyncClient,
) -> None:
    s = await _completion_setup(client)
    published = s["coding_type"]
    assert published["completionSchema"] == _completion_schema(s["reviewer"]["id"])
    read = await client.get(f"/api/v1/task-types/{published['id']}", headers=auth(s["admin_key"]))
    assert read.json()["completionSchema"] == published["completionSchema"]


def _on_complete(actions: list[Any], **extra: Any) -> dict[str, Any]:
    return {"onComplete": {"actions": actions, **extra}}


_ENSURE = {"ensureWork": {"type": "code-review", "key": "k:$.task.id", "title": "t"}}


@pytest.mark.parametrize(
    "document",
    [
        {"somethingElse": {}},
        {"onComplete": {"when": ["$.task.id"]}},
        _on_complete([]),
        _on_complete([{"completeTask": {}}]),
        _on_complete([{"invokeSkill": {"skill": "git.merge@1"}}]),
        _on_complete([{"comment": {"body": "$.approval.comment"}}]),
        _on_complete([{"comment": {"body": "$.invocation.status"}}]),
        _on_complete([_ENSURE], when=["$.approval.id"]),
        _on_complete([_ENSURE], when=["not an expression"]),
        _on_complete([_ENSURE], when=[]),
        _on_complete([_ENSURE], extra=1),
    ],
)
async def test_invalid_completion_schema_is_refused_at_publication(
    client: httpx.AsyncClient, document: dict[str, Any]
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await _create_type(client, admin_key, "coding-task", completionSchema=document)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_completion_schema"


async def test_a_task_closed_by_an_approval_outcome_files_its_work_as_the_decider(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    """completeTask of an outcome is a completion like any other: the type's
    work after completion follows it, with the decider's authority."""
    approved = {"gates": {"default": {"outcomes": {"approved": [{"completeTask": {}}]}}}}
    s = await _review_setup(client, schema=approved)
    after = _on_complete([{"comment": {"body": "Closed: $.task.publicId"}}])
    republished = await _create_type(
        client, s["admin_key"], "code-review", approvalSchema=approved, completionSchema=after
    )
    assert republished.status_code == 201, republished.text
    # The review under test must pin the version that declares the work.
    review = await create_task(
        client,
        s["admin_key"],
        title="Review again",
        typeKey="code-review",
        assigneeId=s["reviewer"]["id"],
    )
    gate = await client.post(
        "/api/v1/approvals",
        json={"task": review["id"], "assignedPrincipalId": s["reviewer"]["id"], "gate": True},
        headers=auth(s["admin_key"]),
    )
    assert gate.status_code == 201, gate.text

    await _decide(client, s["reviewer_key"], gate.json()["id"], "approve")
    await worker.run_once()

    closed = await _task(client, s["admin_key"], review["id"])
    assert closed["systemStatusCategory"] == "terminal_success"
    comments = (
        await client.get(f"/api/v1/tasks/{review['id']}/comments", headers=auth(s["admin_key"]))
    ).json()["items"]
    assert [(c["body"], c["authorPrincipalId"]) for c in comments] == [
        (f"Closed: {review['publicId']}", s["reviewer"]["id"])
    ]
