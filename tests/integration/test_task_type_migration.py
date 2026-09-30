"""Moving open tasks to another version of their type (ADR-0048, amendment 2026-09-30).

A task pins the version it was created against; ``:migrate-type`` moves the
pin deliberately, and ``:migrate-tasks`` does it for every open task of a
version. A refused migration changes nothing — not the type, not the status,
not the version.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from platform_auth import ObjectPage, PolicyDecision
from sqlalchemy import Engine, text

from control_plane.application.authorization import AuthContext, Authorizer, configure_authorizer
from control_plane.application.commands import task_type_migration as migration_commands
from control_plane.domain.errors import AuthorizationError
from tests.helpers import (
    auth,
    backdate_expiry,
    claim_task,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    open_session,
)
from tests.integration.test_task_types_v08 import QUESTION_LIFECYCLE

# v2 of "question": ``investigating`` is renamed ``working``; the rest stays.
RENAMED_LIFECYCLE: dict[str, Any] = {
    "initialStatus": "asked",
    "statuses": [
        {"key": "asked", "category": "backlog"},
        {"key": "working", "category": "active"},
        {"key": "waiting", "category": "blocked"},
        {"key": "answered", "category": "terminal_success"},
        {"key": "dropped", "category": "terminal_cancelled"},
    ],
    "transitions": [
        {"from": "asked", "to": ["working", "dropped"]},
        {"from": "working", "to": ["asked", "waiting", "answered", "dropped"]},
        {"from": "waiting", "to": ["working", "dropped"]},
    ],
    "claimStatus": "working",
    "releaseStatus": "asked",
}

HUMAN = {"key": "looked-at", "kind": "human", "description": "A person has looked at it"}
REVIEW = {"key": "review", "kind": "human", "description": "A person reviews the work"}

# What a runner's key carries, plus the right to manage task types.
RUNNER = ["sessions.open", "tasks.read", "tasks.write", "tasks.claim"]


async def _type(
    client: httpx.AsyncClient, key: str, lifecycle: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/task-types",
        json={
            "key": "question",
            "displayName": "Question",
            "lifecycleSchema": lifecycle,
            **extra,
        },
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def _get(client: httpx.AsyncClient, key: str, ref: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{ref}", headers=auth(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _migrate(
    client: httpx.AsyncClient,
    key: str,
    task: dict[str, Any],
    body: dict[str, Any] | None = None,
    *,
    version: int | None = None,
) -> httpx.Response:
    return await client.post(
        f"/api/v1/tasks/{task['id']}:migrate-type",
        json=body or {},
        headers={
            **auth(key),
            "If-Match": f'"task-{task["version"] if version is None else version}"',
        },
    )


async def _migrate_tasks(
    client: httpx.AsyncClient, key: str, type_id: str, body: dict[str, Any] | None = None
) -> httpx.Response:
    return await client.post(
        f"/api/v1/task-types/{type_id}:migrate-tasks", json=body or {}, headers=auth(key)
    )


def _migrations(sync_engine: Engine, task_id: str) -> list[dict[str, Any]]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT payload FROM events WHERE event_type = 'task.type_migrated' "
                "AND entity_id = :task ORDER BY sequence"
            ),
            {"task": task_id},
        ).all()
    return [dict(row.payload) for row in rows]


def _assert_unchanged(before: dict[str, Any], after: dict[str, Any]) -> None:
    for field in ("typeId", "typeVersion", "status", "systemStatusCategory", "version"):
        assert after[field] == before[field], field


async def _setup(client: httpx.AsyncClient) -> tuple[str, dict[str, Any]]:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    v1 = await _type(client, admin_key, QUESTION_LIFECYCLE)
    return admin_key, v1


# --- one task -----------------------------------------------------------------


async def test_migrate_moves_the_task_to_the_newest_active_version(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key, v1 = await _setup(client)
    task = await create_task(client, key, typeKey="question")
    await _type(client, key, QUESTION_LIFECYCLE, instructions="Before handing in: run ruff.")
    v3 = await _type(client, key, QUESTION_LIFECYCLE)
    # The newest ACTIVE one: a deprecated v4 is not the default target.
    v4 = await _type(client, key, QUESTION_LIFECYCLE)
    deprecated = await client.post(f"/api/v1/task-types/{v4['id']}:deprecate", headers=auth(key))
    assert deprecated.status_code == 200, deprecated.text

    response = await _migrate(client, key, task)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["typeId"], body["typeVersion"]) == (v3["id"], 3)
    assert (body["status"], body["systemStatusCategory"]) == ("asked", "backlog")
    assert body["version"] == task["version"] + 1
    assert (await _get(client, key, task["id"]))["typeVersion"] == 3
    assert _migrations(sync_engine, task["id"]) == [
        {
            "publicId": task["publicId"],
            "typeKey": "question",
            "fromTypeVersion": v1["version"],
            "typeVersion": 3,
            "fromStatus": "asked",
            "status": "asked",
            "systemStatusCategory": "backlog",
            "trigger": "task",
            "version": task["version"] + 1,
        }
    ]


async def test_migrate_to_an_explicit_version_and_back(client: httpx.AsyncClient) -> None:
    key, v1 = await _setup(client)
    task = await create_task(client, key, typeKey="question")
    await _type(client, key, QUESTION_LIFECYCLE)

    forward = await _migrate(client, key, task, {"typeVersion": 2})
    assert forward.status_code == 200, forward.text
    back = await _migrate(client, key, forward.json(), {"typeVersion": 1})
    assert back.status_code == 200, back.text
    assert back.json()["typeId"] == v1["id"]


async def test_migrate_to_the_version_the_task_carries_is_a_no_op(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key, _ = await _setup(client)
    task = await create_task(client, key, typeKey="question")

    response = await _migrate(client, key, task)

    assert response.status_code == 200, response.text
    assert response.json()["version"] == task["version"]
    assert _migrations(sync_engine, task["id"]) == []


async def test_migrate_to_an_unknown_version_is_not_found(client: httpx.AsyncClient) -> None:
    key, _ = await _setup(client)
    task = await create_task(client, key, typeKey="question")

    response = await _migrate(client, key, task, {"typeVersion": 7})

    assert response.status_code == 404, response.text


async def test_migrate_needs_if_match_and_the_current_version(client: httpx.AsyncClient) -> None:
    key, _ = await _setup(client)
    task = await create_task(client, key, typeKey="question")
    await _type(client, key, QUESTION_LIFECYCLE)

    missing = await client.post(
        f"/api/v1/tasks/{task['id']}:migrate-type", json={}, headers=auth(key)
    )
    assert missing.status_code == 428, missing.text
    stale = await _migrate(client, key, task, version=task["version"] + 5)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "version_conflict"


async def test_status_missing_in_the_target_is_409_until_it_is_mapped(
    client: httpx.AsyncClient,
) -> None:
    key, _ = await _setup(client)
    created = await create_task(client, key, typeKey="question")
    moved = await client.patch(
        f"/api/v1/tasks/{created['id']}",
        json={"status": "investigating"},
        headers={**auth(key), "If-Match": f'"task-{created["version"]}"'},
    )
    assert moved.status_code == 200, moved.text
    task = moved.json()
    await _type(client, key, RENAMED_LIFECYCLE)

    refused = await _migrate(client, key, task)
    assert refused.status_code == 409
    error = refused.json()["error"]
    assert error["code"] == "incompatible_status"
    assert error["details"]["statusKey"] == "investigating"
    _assert_unchanged(task, await _get(client, key, task["id"]))

    mapped = await _migrate(client, key, task, {"statusMap": {"investigating": "working"}})
    assert mapped.status_code == 200, mapped.text
    assert (mapped.json()["status"], mapped.json()["systemStatusCategory"]) == (
        "working",
        "active",
    )


async def test_status_map_into_an_undeclared_or_terminal_status_is_422(
    client: httpx.AsyncClient,
) -> None:
    key, _ = await _setup(client)
    task = await create_task(client, key, typeKey="question")
    await _type(client, key, RENAMED_LIFECYCLE)

    for status_map in ({"asked": "investigating"}, {"asked": "answered"}):
        refused = await _migrate(client, key, task, {"statusMap": status_map})
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "invalid_status_map"
    _assert_unchanged(task, await _get(client, key, task["id"]))


async def test_live_claim_refuses_the_migration_even_for_its_holder(
    client: httpx.AsyncClient,
) -> None:
    key, _ = await _setup(client)
    task = await create_task(client, key, typeKey="question")
    await _type(client, key, QUESTION_LIFECYCLE)
    _, agent_key = await create_agent_with_key(
        client, key, name="coder", permissions=[*RUNNER, "task_types.manage"]
    )
    work_session = await open_session(client, agent_key)
    claimed = await claim_task(client, agent_key, task["id"], work_session["id"])
    assert claimed.status_code == 200, claimed.text
    before = await _get(client, key, task["id"])

    for caller in (key, agent_key):
        refused = await _migrate(client, caller, before)
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "task_claimed"
    _assert_unchanged(before, await _get(client, key, task["id"]))


async def test_closed_task_keeps_its_version(client: httpx.AsyncClient) -> None:
    key, _ = await _setup(client)
    created = await create_task(client, key, typeKey="question")
    dropped = await client.patch(
        f"/api/v1/tasks/{created['id']}",
        json={"status": "dropped"},
        headers={**auth(key), "If-Match": f'"task-{created["version"]}"'},
    )
    assert dropped.status_code == 200, dropped.text
    await _type(client, key, QUESTION_LIFECYCLE)

    refused = await _migrate(client, key, dropped.json())

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "task_terminal"


async def test_custom_fields_must_satisfy_the_target_field_schema(
    client: httpx.AsyncClient,
) -> None:
    key, _ = await _setup(client)
    task = await create_task(client, key, typeKey="question", customFields={"area": "core"})
    await _type(
        client,
        key,
        QUESTION_LIFECYCLE,
        fieldSchema={
            "type": "object",
            "properties": {"repositoryKey": {"type": "string"}},
            "required": ["repositoryKey"],
        },
    )

    refused = await _migrate(client, key, task)

    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "custom_fields_invalid"
    _assert_unchanged(task, await _get(client, key, task["id"]))


async def test_own_acceptance_must_not_collide_with_the_target_checks(
    client: httpx.AsyncClient,
) -> None:
    key, _ = await _setup(client)
    task = await create_task(client, key, typeKey="question", acceptance=[HUMAN])
    await _type(client, key, QUESTION_LIFECYCLE, acceptance=[HUMAN])

    refused = await _migrate(client, key, task)

    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "invalid_acceptance"
    _assert_unchanged(task, await _get(client, key, task["id"]))


async def test_open_verification_attempt_refuses_the_migration(
    client: httpx.AsyncClient,
) -> None:
    key, _ = await _setup(client)
    # The system type: its lifecycle walks todo -> done directly.
    task = await create_task(client, key, acceptance=[HUMAN])
    handed_in = await client.post(
        f"/api/v1/tasks/{task['id']}:complete",
        json={},
        headers={**auth(key), "If-Match": f'"task-{task["version"]}"'},
    )
    assert handed_in.status_code == 200, handed_in.text
    assert handed_in.json()["verification"] is not None
    next_version = await client.post(
        "/api/v1/task-types", json={"key": "task", "displayName": "Task"}, headers=auth(key)
    )
    assert next_version.status_code == 201, next_version.text
    before = await _get(client, key, task["id"])

    refused = await _migrate(client, key, before)

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "verification_pending"


async def test_pending_gate_approval_refuses_the_migration(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    await _type(client, key, QUESTION_LIFECYCLE)
    task = await create_task(client, key, typeKey="question")
    approval = await client.post(
        "/api/v1/approvals",
        json={
            "task": task["id"],
            "gate": True,
            "assignedPrincipalId": boot["adminPrincipal"]["id"],
        },
        headers=auth(key),
    )
    assert approval.status_code == 201, approval.text
    await _type(client, key, QUESTION_LIFECYCLE)

    refused = await _migrate(client, key, task)

    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["code"] == "approval_pending"
    assert error["details"]["approvals"] == [approval.json()["id"]]

    decided = await client.post(
        f"/api/v1/approvals/{approval.json()['id']}:reject", json={}, headers=auth(key)
    )
    assert decided.status_code == 200, decided.text
    assert (await _migrate(client, key, task)).status_code == 200


async def test_migration_needs_tasks_write(client: httpx.AsyncClient) -> None:
    key, _ = await _setup(client)
    task = await create_task(client, key, typeKey="question")
    await _type(client, key, QUESTION_LIFECYCLE)
    _, reader_key = await create_agent_with_key(
        client, key, name="reader", permissions=["tasks.read"]
    )

    refused = await _migrate(client, reader_key, task)

    assert refused.status_code == 403, refused.text
    _assert_unchanged(task, await _get(client, key, task["id"]))


async def test_tasks_write_alone_cannot_drop_the_checks_of_the_type(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Review of 2026-09-30, B1: v1 has no checks, v2 a human ``review``. A
    runner's key (``tasks.read/write/claim``) must not move its task back to
    v1 and then close it without review (CP-ADR-0067: the type's checks are a
    floor). Nor forward: moving the pin is a type manager's decision."""
    key, _ = await _setup(client)
    await _type(client, key, QUESTION_LIFECYCLE, acceptance=[REVIEW])
    task = await create_task(client, key, typeKey="question")
    assert task["typeVersion"] == 2
    _, runner_key = await create_agent_with_key(client, key, name="coder", permissions=RUNNER)

    for body in ({"typeVersion": 1}, {}):
        refused = await _migrate(client, runner_key, task, body)
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["details"]["required"] == ["task_types.manage"]
    _assert_unchanged(task, await _get(client, key, task["id"]))
    assert _migrations(sync_engine, task["id"]) == []


async def test_the_default_target_never_rolls_back(client: httpx.AsyncClient) -> None:
    key, v1 = await _setup(client)
    v2 = await _type(client, key, QUESTION_LIFECYCLE)
    task = await create_task(client, key, typeKey="question")
    assert task["typeVersion"] == 2
    deprecated = await client.post(f"/api/v1/task-types/{v2['id']}:deprecate", headers=auth(key))
    assert deprecated.status_code == 200, deprecated.text

    # v1 is now the newest active version, and older than the task's v2.
    refused = await _migrate(client, key, task)
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "invalid_migration_target"
    assert (error["details"]["typeVersion"], error["details"]["newestActiveVersion"]) == (2, 1)
    _assert_unchanged(task, await _get(client, key, task["id"]))
    bulk = await _migrate_tasks(client, key, v2["id"])
    assert bulk.status_code == 422, bulk.text
    assert bulk.json()["error"]["code"] == "invalid_migration_target"
    assert (await _get(client, key, task["id"]))["typeVersion"] == 2

    # Named explicitly, a rollback is the type manager's call.
    back = await _migrate(client, key, task, {"typeVersion": 1})
    assert back.status_code == 200, back.text
    assert back.json()["typeId"] == v1["id"]


async def test_status_map_key_unknown_to_the_source_is_422(client: httpx.AsyncClient) -> None:
    key, v1 = await _setup(client)
    task = await create_task(client, key, typeKey="question")
    await _type(client, key, RENAMED_LIFECYCLE)

    # "working" is a status of the target, not of the task's v1: a typo'd or
    # misplaced key would otherwise silently map nothing.
    body = {"statusMap": {"working": "asked"}}
    refused = await _migrate(client, key, task, body)
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert (error["code"], error["details"]["field"]) == ("invalid_status_map", "statusMap.working")
    bulk = await _migrate_tasks(client, key, v1["id"], body)
    assert bulk.status_code == 422, bulk.text
    assert bulk.json()["error"]["code"] == "invalid_status_map"
    _assert_unchanged(task, await _get(client, key, task["id"]))


async def test_running_run_refuses_the_migration_after_its_claim_expired(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key, v1 = await _setup(client)
    task = await create_task(client, key, typeKey="question")
    await _type(client, key, QUESTION_LIFECYCLE)
    _, agent_key = await create_agent_with_key(client, key, name="coder", permissions=RUNNER)
    work_session = await open_session(client, agent_key)
    claim = (await claim_task(client, agent_key, task["id"], work_session["id"])).json()
    started = await client.post(
        f"/api/v1/tasks/{task['id']}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(agent_key),
    )
    assert started.status_code == 201, started.text
    # The lease is gone, the runner is still at work with v1's instructions.
    backdate_expiry(sync_engine, "task_claims", claim["id"])
    before = await _get(client, key, task["id"])

    refused = await _migrate(client, key, before)
    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert (error["code"], error["details"]["runId"]) == ("run_in_progress", started.json()["id"])
    _assert_unchanged(before, await _get(client, key, task["id"]))
    bulk = await _migrate_tasks(client, key, v1["id"])
    assert bulk.status_code == 200, bulk.text
    assert [(s["taskId"], s["code"]) for s in bulk.json()["skipped"]] == [
        (task["id"], "run_in_progress")
    ]


@dataclass
class HidesTask:
    """A PDP that allows every check except any action on one task."""

    hidden: str

    async def check(
        self, ctx, action, resource, *, contextual=(), on_behalf_of=None, consistency="default"
    ):  # type: ignore[no-untyped-def]
        return PolicyDecision(
            allowed=not resource.key.endswith(self.hidden),
            reason_code="allowed",
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
def restore_authorizer() -> Any:
    yield
    configure_authorizer(Authorizer(None, "local"))


async def test_task_the_policy_hides_is_refused_alone_and_left_out_in_bulk(
    client: httpx.AsyncClient, app: Any, restore_authorizer: None
) -> None:
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    v1 = await _type(client, key, QUESTION_LIFECYCLE)
    hidden = await create_task(client, key, typeKey="question", title="hidden")
    visible = await create_task(client, key, typeKey="question", title="visible")
    await _type(client, key, QUESTION_LIFECYCLE)

    configure_authorizer(Authorizer(HidesTask(hidden=hidden["id"]), "policy"))
    ctx = AuthContext(
        tenant_id=uuid.UUID(boot["tenant"]["id"]),
        principal_id=uuid.UUID(boot["adminPrincipal"]["id"]),
        principal_kind="human",
        api_key_id=uuid.uuid4(),
        permissions=frozenset(),
        iam_principal_id=uuid.uuid4(),
    )
    async with app.state.session_factory() as session:
        with pytest.raises(AuthorizationError):
            await migration_commands.migrate_task_type(
                session, ctx, task_ref=hidden["id"], expected_version=hidden["version"]
            )
        await session.rollback()
        page = await migration_commands.migrate_type_tasks(
            session, ctx, type_id=uuid.UUID(v1["id"])
        )
        await session.rollback()

    # Neither migrated nor reported as skipped: as if it did not exist.
    assert [m["taskId"] for m in page["migrated"]] == [visible["id"]]
    assert page["skipped"] == []


async def test_task_of_another_tenant_is_not_found(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    from tests.helpers import make_tenant_directly

    key, _ = await _setup(client)
    task = await create_task(client, key, typeKey="question")
    await _type(client, key, QUESTION_LIFECYCLE)
    _, other_key = make_tenant_directly(sync_engine, "other")

    refused = await _migrate(client, other_key, task)

    assert refused.status_code == 404, refused.text


# --- every open task of a version ---------------------------------------------


async def test_migrate_tasks_moves_open_tasks_and_reports_the_refused(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key, v1 = await _setup(client)
    free = await create_task(client, key, typeKey="question", title="free")
    held = await create_task(client, key, typeKey="question", title="held")
    closed = await create_task(client, key, typeKey="question", title="closed")
    dropped = await client.patch(
        f"/api/v1/tasks/{closed['id']}",
        json={"status": "dropped"},
        headers={**auth(key), "If-Match": f'"task-{closed["version"]}"'},
    )
    assert dropped.status_code == 200, dropped.text
    _, agent_key = await create_agent_with_key(client, key, name="coder")
    work_session = await open_session(client, agent_key)
    assert (await claim_task(client, agent_key, held["id"], work_session["id"])).status_code == 200
    v2 = await _type(client, key, RENAMED_LIFECYCLE)

    response = await _migrate_tasks(
        client, key, v1["id"], {"statusMap": {"investigating": "working"}}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["typeKey"], body["fromTypeVersion"], body["typeVersion"]) == ("question", 1, 2)
    assert body["typeId"] == v2["id"]
    assert [m["taskId"] for m in body["migrated"]] == [free["id"]]
    assert body["migrated"][0]["status"] == "asked"
    # The claimed task stays where it was and says why; the closed one is not
    # an open task of the version at all.
    assert [(s["taskId"], s["code"]) for s in body["skipped"]] == [(held["id"], "task_claimed")]
    assert body["nextCursor"] is None
    assert (await _get(client, key, free["id"]))["typeVersion"] == 2
    assert (await _get(client, key, held["id"]))["typeVersion"] == 1
    assert (await _get(client, key, closed["id"]))["typeVersion"] == 1
    assert _migrations(sync_engine, free["id"])[0]["trigger"] == "bulk"


async def test_migrate_tasks_pages_by_cursor(client: httpx.AsyncClient) -> None:
    key, v1 = await _setup(client)
    tasks = [await create_task(client, key, typeKey="question") for _ in range(3)]
    await _type(client, key, QUESTION_LIFECYCLE)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        body: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            body["cursor"] = cursor
        response = await _migrate_tasks(client, key, v1["id"], body)
        assert response.status_code == 200, response.text
        page = response.json()
        seen += [m["taskId"] for m in page["migrated"]]
        cursor = page["nextCursor"]
        if cursor is None:
            break

    assert sorted(seen) == sorted(t["id"] for t in tasks)
    # Nothing left: a repeated call finds no open task of v1.
    again = await _migrate_tasks(client, key, v1["id"])
    assert again.json()["migrated"] == []


async def test_migrate_tasks_refuses_a_bad_map_before_moving_anything(
    client: httpx.AsyncClient,
) -> None:
    key, v1 = await _setup(client)
    task = await create_task(client, key, typeKey="question")
    await _type(client, key, RENAMED_LIFECYCLE)

    refused = await _migrate_tasks(client, key, v1["id"], {"statusMap": {"asked": "answered"}})

    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "invalid_status_map"
    assert (await _get(client, key, task["id"]))["typeVersion"] == 1


async def test_migrate_tasks_to_the_same_version_is_refused(client: httpx.AsyncClient) -> None:
    key, v1 = await _setup(client)

    refused = await _migrate_tasks(client, key, v1["id"])

    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "invalid_migration_target"


async def test_migrate_tasks_needs_task_types_manage(client: httpx.AsyncClient) -> None:
    key, v1 = await _setup(client)
    task = await create_task(client, key, typeKey="question")
    await _type(client, key, QUESTION_LIFECYCLE)
    _, agent_key = await create_agent_with_key(client, key, name="coder")

    refused = await _migrate_tasks(client, agent_key, v1["id"])

    assert refused.status_code == 403, refused.text
    assert (await _get(client, key, task["id"]))["typeVersion"] == 1


async def test_migrate_tasks_of_an_unknown_version_is_not_found(
    client: httpx.AsyncClient,
) -> None:
    key, _ = await _setup(client)

    response = await _migrate_tasks(client, key, "00000000-0000-0000-0000-000000000001")

    assert response.status_code == 404, response.text


async def test_migrate_tasks_leaves_a_task_that_moved_after_the_page_was_read(
    client: httpx.AsyncClient, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, v1 = await _setup(client)
    task = await create_task(client, key, typeKey="question")
    await _type(client, key, QUESTION_LIFECYCLE)
    v3 = await _type(client, key, QUESTION_LIFECYCLE)
    lock = migration_commands.resolve_task_for_update

    async def moved_meanwhile(session: Any, ctx: Any, ref: str) -> Any:
        # Another writer moves the task to v3 between the page and the lock.
        with sync_engine.begin() as conn:
            conn.execute(
                text("UPDATE tasks SET type_id = :type WHERE id = :task"),
                {"type": v3["id"], "task": ref},
            )
        return await lock(session, ctx, ref)

    monkeypatch.setattr(migration_commands, "resolve_task_for_update", moved_meanwhile)

    response = await _migrate_tasks(client, key, v1["id"], {"toVersion": 2})

    assert response.status_code == 200, response.text
    assert (response.json()["migrated"], response.json()["skipped"]) == ([], [])
    assert (await _get(client, key, task["id"]))["typeVersion"] == 3
    assert _migrations(sync_engine, task["id"]) == []
