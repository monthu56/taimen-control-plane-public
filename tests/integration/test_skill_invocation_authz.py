"""M2.1 review: who may invoke a skill for what (ADR-0056 §2, §4, amendment).

Negative scenarios first: each test pins one way a caller could reach further
than its rights — another's Work item, a spent or stale approval, a child run
stepping outside its ceiling, a workspace-scoped binding asked at tenant level.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.commands import skill_invocations
from control_plane.domain.errors import AuthorizationError
from tests.helpers import (
    assign_skill,
    auth,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
    open_session,
)
from tests.integration.test_child_run_handle import claim_and_run, launch
from tests.integration.test_skill_invocations_m21 import (
    claim,
    invoke,
    published,
)

CALLER = ["skills.invoke", "tasks.read", "tasks.write"]
EXECUTOR = ["skills.execute", "sessions.open"]
RUNNER = [
    "sessions.open",
    "tasks.read",
    "tasks.write",
    "tasks.claim",
    "skills.invoke",
    "artifacts.write",
]


@pytest.fixture
async def boot(client: httpx.AsyncClient) -> dict[str, Any]:
    body = await do_bootstrap(client)
    admin = body["apiKey"]["key"]
    caller, caller_key = await create_agent_with_key(
        client, admin, name="caller", permissions=CALLER
    )
    _, executor_key = await create_agent_with_key(
        client, admin, name="executor", permissions=EXECUTOR
    )
    return {
        "admin": admin,
        "adminId": body["adminPrincipal"]["id"],
        "caller": caller_key,
        "callerId": caller["id"],
        "executor": executor_key,
    }


def scoped_binding(
    monkeypatch: pytest.MonkeyPatch, principal_id: str, workspace_id: str, permissions: set[str]
) -> list[str]:
    """Simulate a PDP binding that grants ``permissions`` on one workspace only.

    Local authorization is flat, so the scoped case is reproduced at the one
    seam the command uses. Every question about those permissions is answered
    by the resource it names; the returned list records what was asked.
    """
    real: Callable[..., Any] = skill_invocations.authorize
    asked: list[str] = []

    async def authorize(ctx: Any, *any_of: Any, resource: Any = None, **kwargs: Any) -> None:
        names = {permission.value for permission in any_of}
        if str(ctx.principal_id) == principal_id and names & permissions:
            key = resource.key if resource is not None else f"tenant:{ctx.tenant_id}"
            asked.append(f"{','.join(sorted(names))}@{key}")
            if key != f"workspace:{workspace_id}":
                raise AuthorizationError(details={"required": sorted(names), "resource": key})
            return
        await real(ctx, *any_of, resource=resource, **kwargs)

    monkeypatch.setattr(skill_invocations, "authorize", authorize)
    return asked


def invocation_count(sync_engine: Engine) -> int:
    with sync_engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM skill_invocations")).scalar_one())


async def approved_gate(client: httpx.AsyncClient, boot: dict[str, Any], task_id: str) -> str:
    approval = await client.post(
        "/api/v1/approvals",
        json={"task": task_id, "assignedPrincipalId": boot["adminId"], "gate": True},
        headers=auth(boot["admin"]),
    )
    assert approval.status_code == 201, approval.text
    decided = await client.post(
        f"/api/v1/approvals/{approval.json()['id']}:approve", headers=auth(boot["admin"])
    )
    assert decided.status_code == 200, decided.text
    return str(approval.json()["id"])


# --- 1. the Work item named by the call ---------------------------------------


async def test_referencing_a_task_needs_tasks_write(
    client: httpx.AsyncClient, boot: dict[str, Any], sync_engine: Engine
) -> None:
    await published(client, boot["admin"])
    task = await create_task(client, boot["admin"], title="Someone else's")
    _, reader = await create_agent_with_key(
        client, boot["admin"], name="reader", permissions=["skills.invoke", "tasks.read"]
    )

    response = await invoke(client, reader, "repo.search", {"query": "x"}, taskId=task["id"])
    assert response.status_code == 403, response.text
    assert invocation_count(sync_engine) == 0

    # Without a Work item the same caller may still invoke.
    response = await invoke(client, reader, "repo.search", {"query": "x"})
    assert response.status_code == 201, response.text


async def test_task_write_is_checked_on_the_task_workspace(
    client: httpx.AsyncClient,
    boot: dict[str, Any],
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = boot["admin"]
    await published(client, admin)
    await published(client, admin, "crm.update", side_effects="external_write", risk_level="high")
    own = await create_workspace(client, admin, "own")
    foreign = await create_workspace(client, admin, "foreign")
    own_task = await create_task(client, admin, title="Mine", workspaceId=own["id"])
    foreign_task = await create_task(client, admin, title="Theirs", workspaceId=foreign["id"])
    foreign_gate = await approved_gate(client, boot, foreign_task["id"])
    asked = scoped_binding(monkeypatch, boot["callerId"], own["id"], {"tasks.write"})

    # Another workspace's task and its approved gate are not a basis to act on.
    response = await invoke(
        client,
        boot["caller"],
        "crm.update",
        {"query": "x"},
        taskId=foreign_task["id"],
        approvalId=foreign_gate,
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "permission_denied"
    response = await invoke(
        client, boot["caller"], "repo.search", {"query": "x"}, taskId=foreign_task["id"]
    )
    assert response.status_code == 403
    assert invocation_count(sync_engine) == 0
    assert f"tasks.write@workspace:{foreign['id']}" in asked

    response = await invoke(
        client, boot["caller"], "repo.search", {"query": "x"}, taskId=own_task["id"]
    )
    assert response.status_code == 201, response.text


# --- 2. approval as a basis ---------------------------------------------------


async def test_an_approval_authorizes_one_call_per_skill_version(
    client: httpx.AsyncClient, boot: dict[str, Any], sync_engine: Engine
) -> None:
    admin, caller = boot["admin"], boot["caller"]
    await published(client, admin, "crm.update", side_effects="external_write", risk_level="high")
    task = await create_task(client, admin, title="Update CRM")
    gate = await approved_gate(client, boot, task["id"])

    first = await invoke(
        client, caller, "crm.update", {"query": "a"}, taskId=task["id"], approvalId=gate
    )
    assert first.status_code == 201, first.text
    assert first.json()["authorizationBasis"]["approvalId"] == gate

    again = await invoke(
        client, caller, "crm.update", {"query": "b"}, taskId=task["id"], approvalId=gate
    )
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "approval_already_used"
    assert invocation_count(sync_engine) == 1

    # A retry of the SAME call is not a second use: it returns the first one.
    await published(
        client,
        admin,
        "crm.update",
        version="2.0.0",
        side_effects="external_write",
        risk_level="high",
        idempotency="required",
    )
    keyed = await invoke(
        client,
        caller,
        "crm.update@2.0.0",
        {"query": "a"},
        taskId=task["id"],
        approvalId=gate,
        idempotencyKey="crm-1",
    )
    assert keyed.status_code == 201, keyed.text
    retry = await invoke(
        client,
        caller,
        "crm.update@2.0.0",
        {"query": "a"},
        taskId=task["id"],
        approvalId=gate,
        idempotencyKey="crm-1",
    )
    assert retry.status_code == 200
    assert retry.json()["id"] == keyed.json()["id"]


async def test_an_approval_does_not_act_for_a_closed_task(
    client: httpx.AsyncClient, boot: dict[str, Any], sync_engine: Engine
) -> None:
    admin = boot["admin"]
    await published(client, admin, "crm.update", side_effects="external_write", risk_level="high")
    task = await create_task(client, admin, title="Update CRM")
    gate = await approved_gate(client, boot, task["id"])
    current = await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin))
    closed = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "cancelled"},
        headers={**auth(admin), "If-Match": current.headers["etag"]},
    )
    assert closed.status_code == 200, closed.text

    response = await invoke(
        client, boot["caller"], "crm.update", {"query": "x"}, taskId=task["id"], approvalId=gate
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "task_terminal"
    assert invocation_count(sync_engine) == 0


# --- 3. the ceiling of a child run --------------------------------------------


@pytest.fixture
async def child(client: httpx.AsyncClient, boot: dict[str, Any]) -> dict[str, Any]:
    """A root run of ``parent`` and a child run of ``child`` bounded by a handle.

    The child's API key holds ``artifacts.write``; its handle does not grant it
    and grants only the ``repo.search`` skill.
    """
    admin = boot["admin"]
    parent, parent_key = await create_agent_with_key(
        client, admin, name="parent", permissions=RUNNER
    )
    child, child_key = await create_agent_with_key(client, admin, name="child", permissions=RUNNER)
    search = await published(client, admin)
    admin_skill = await published(
        client, admin, "repo.admin", requiredPermissions=["artifacts.write"]
    )
    other = await published(client, admin, "repo.other")
    for principal in (parent, child):
        for skill in (search, admin_skill, other):
            await assign_skill(client, admin, principal["id"], skill["id"])

    task = await create_task(client, admin, title="Parent work")
    _, parent_run = await claim_and_run(client, parent_key, task["id"])
    handle = (
        await launch(
            client,
            parent_key,
            parent_run["id"],
            grant={
                "permissions": [
                    "sessions.open",
                    "tasks.read",
                    "tasks.write",
                    "tasks.claim",
                    "skills.invoke",
                ],
                "skills": ["repo.search@1.0.0", "repo.admin@1.0.0"],
            },
        )
    ).json()["childHandle"]
    _, child_run = await claim_and_run(client, child_key, handle["childTaskId"])
    return {
        "parentKey": parent_key,
        "parentRun": parent_run,
        "childKey": child_key,
        "childRun": child_run,
        "childTaskId": handle["childTaskId"],
    }


async def test_required_permissions_are_bounded_by_the_child_grant(
    client: httpx.AsyncClient, child: dict[str, Any], sync_engine: Engine
) -> None:
    # The child's key holds artifacts.write; its grant does not.
    response = await invoke(
        client,
        child["childKey"],
        "repo.admin",
        {"query": "x"},
        runId=child["childRun"]["id"],
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "child_grant_exceeded"
    assert response.json()["error"]["details"]["required"] == "artifacts.write"

    # The parent, a root run, is bounded only by its key.
    response = await invoke(
        client,
        child["parentKey"],
        "repo.admin",
        {"query": "x"},
        runId=child["parentRun"]["id"],
    )
    assert response.status_code == 201, response.text


async def test_a_child_run_cannot_drop_its_run_id_to_escape_the_ceiling(
    client: httpx.AsyncClient, child: dict[str, Any], sync_engine: Engine
) -> None:
    for extra in ({}, {"taskId": child["childTaskId"]}):
        response = await invoke(client, child["childKey"], "repo.admin", {"query": "x"}, **extra)
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "run_id_required"
        assert response.json()["error"]["details"]["runId"] == child["childRun"]["id"]
    assert invocation_count(sync_engine) == 0


async def test_a_bounded_caller_cannot_name_its_root_run_to_escape_the_ceiling(
    client: httpx.AsyncClient, boot: dict[str, Any], child: dict[str, Any], sync_engine: Engine
) -> None:
    # The child's principal also runs a root run of its own: naming it would
    # carry the full rights of the key while the child run is still going.
    task = await create_task(client, boot["admin"], title="Own root work")
    _, root_run = await claim_and_run(client, child["childKey"], task["id"])
    response = await invoke(
        client, child["childKey"], "repo.admin", {"query": "x"}, runId=root_run["id"]
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "run_id_required"
    assert response.json()["error"]["details"]["runId"] == child["childRun"]["id"]
    assert invocation_count(sync_engine) == 0


async def test_a_finished_run_carries_no_authority(
    client: httpx.AsyncClient, boot: dict[str, Any], child: dict[str, Any], sync_engine: Engine
) -> None:
    fail = await client.post(
        f"/api/v1/runs/{child['childRun']['id']}:fail",
        json={"failureReason": "done"},
        headers=auth(child["childKey"]),
    )
    assert fail.status_code == 200, fail.text
    cancel = await client.post(
        f"/api/v1/runs/{child['parentRun']['id']}:cancel",
        json={"reason": "operator stop"},
        headers=auth(boot["admin"]),
    )
    assert cancel.status_code == 200, cancel.text

    for key, run in (
        (child["childKey"], child["childRun"]),
        (child["parentKey"], child["parentRun"]),
    ):
        response = await invoke(client, key, "repo.search", {"query": "x"}, runId=run["id"])
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "run_not_active"
    assert invocation_count(sync_engine) == 0


async def test_the_tool_policy_of_the_run_narrows_skills(
    client: httpx.AsyncClient,
    boot: dict[str, Any],
    child: dict[str, Any],
    sync_engine: Engine,
) -> None:
    # Not in the handle's skill grant.
    response = await invoke(
        client, child["childKey"], "repo.other", {"query": "x"}, runId=child["childRun"]["id"]
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "child_grant_exceeded"

    # Granted: passes.
    response = await invoke(
        client, child["childKey"], "repo.search", {"query": "x"}, runId=child["childRun"]["id"]
    )
    assert response.status_code == 201, response.text

    # A skill never assigned to the principal is outside the policy of any run.
    await published(client, boot["admin"], "repo.unassigned")
    response = await invoke(
        client,
        child["parentKey"],
        "repo.unassigned",
        {"query": "x"},
        runId=child["parentRun"]["id"],
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "tool_not_authorized"
    assert invocation_count(sync_engine) == 1


# --- 4. requiredPermissions where the Work item lives -------------------------


async def test_required_permissions_are_checked_on_the_task_workspace(
    client: httpx.AsyncClient,
    boot: dict[str, Any],
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = boot["admin"]
    await published(client, admin, "repo.admin", requiredPermissions=["artifacts.write"])
    own = await create_workspace(client, admin, "own")
    foreign = await create_workspace(client, admin, "foreign")
    own_task = await create_task(client, admin, title="Mine", workspaceId=own["id"])
    foreign_task = await create_task(client, admin, title="Theirs", workspaceId=foreign["id"])
    # A runner binding: artifacts.write on its workspace only, nothing tenant-wide.
    asked = scoped_binding(monkeypatch, boot["callerId"], own["id"], {"artifacts.write"})

    response = await invoke(client, boot["caller"], "repo.admin", {"query": "x"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "skill_permission_denied"

    response = await invoke(
        client, boot["caller"], "repo.admin", {"query": "x"}, taskId=foreign_task["id"]
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "skill_permission_denied"
    assert response.json()["error"]["details"]["resource"] == f"workspace:{foreign['id']}"
    assert invocation_count(sync_engine) == 0

    response = await invoke(
        client, boot["caller"], "repo.admin", {"query": "x"}, taskId=own_task["id"]
    )
    assert response.status_code == 201, response.text
    assert f"artifacts.write@workspace:{own['id']}" in asked


# --- 5. smaller review points -------------------------------------------------


async def test_idempotent_repeat_survives_a_disabled_version(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    skill = await published(client, boot["admin"])
    first = await invoke(
        client, boot["caller"], "repo.search@1.0.0", {"query": "x"}, idempotencyKey="k"
    )
    assert first.status_code == 201
    disabled = await client.patch(
        f"/api/v1/skills/{skill['id']}",
        json={"status": "disabled"},
        headers={**auth(boot["admin"]), "If-Match": f'"skill-{skill["rowVersion"]}"'},
    )
    assert disabled.status_code == 200, disabled.text

    again = await invoke(
        client, boot["caller"], "repo.search@1.0.0", {"query": "x"}, idempotencyKey="k"
    )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]
    fresh = await invoke(
        client, boot["caller"], "repo.search@1.0.0", {"query": "x"}, idempotencyKey="k2"
    )
    assert fresh.status_code == 409
    assert fresh.json()["error"]["code"] == "skill_not_invocable"


async def test_rejected_output_is_kept_as_evidence(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    await published(client, boot["admin"])
    created = (await invoke(client, boot["caller"], "repo.search", {"query": "x"})).json()
    lease = (await claim(client, boot["executor"])).json()["invocation"]
    response = await client.post(
        f"/api/v1/skill-invocations/{created['id']}:complete",
        json={"fencingToken": lease["fencingToken"], "output": {"hits": -1}},
        headers=auth(boot["executor"]),
    )
    error = response.json()["error"]
    assert error["code"] == "output_contract_violation"
    assert error["details"]["rejectedOutput"] == {"hits": -1}

    created = (await invoke(client, boot["caller"], "repo.search", {"query": "y"})).json()
    lease = (await claim(client, boot["executor"])).json()["invocation"]
    response = await client.post(
        f"/api/v1/skill-invocations/{created['id']}:complete",
        json={"fencingToken": lease["fencingToken"], "output": {"hits": ["x" * 1000] * 100}},
        headers=auth(boot["executor"]),
    )
    details = response.json()["error"]["details"]
    assert "rejectedOutput" not in details
    assert details["rejectedOutputTruncated"] is True
    assert details["rejectedOutputBytes"] > 100_000


async def test_json_schema_format_is_asserted(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    schema = {
        "type": "object",
        "properties": {"id": {"type": "string", "format": "uuid"}},
        "required": ["id"],
    }
    await published(client, boot["admin"], "repo.get", inputs=schema)
    response = await invoke(client, boot["caller"], "repo.get", {"id": "not-a-uuid"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_skill_inputs"
    response = await invoke(
        client, boot["caller"], "repo.get", {"id": "8b0f6a2e-3c1d-4e5f-9a7b-1c2d3e4f5a6b"}
    )
    assert response.status_code == 201, response.text


async def test_legacy_config_is_hidden_without_org_read(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    legacy = await client.post(
        "/api/v1/skills",
        json={"name": "legacy", "protocol": "http", "config": {"url": "https://internal.example"}},
        headers=auth(boot["admin"]),
    )
    assert legacy.status_code == 201, legacy.text

    as_caller = await client.get("/api/v1/skills/legacy", headers=auth(boot["caller"]))
    assert as_caller.status_code == 200
    assert as_caller.json()["config"] == {}
    as_admin = await client.get("/api/v1/skills/legacy", headers=auth(boot["admin"]))
    assert as_admin.json()["config"] == {"url": "https://internal.example"}


async def test_heartbeat_cannot_extend_the_lease_past_the_attempt_deadline(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    await published(client, boot["admin"], timeoutSeconds=5)
    created = (await invoke(client, boot["caller"], "repo.search", {"query": "x"})).json()
    lease = (await claim(client, boot["executor"])).json()["invocation"]
    initial = datetime.fromisoformat(lease["leaseExpiresAt"])

    beat = await client.post(
        f"/api/v1/skill-invocations/{created['id']}:heartbeat",
        json={"fencingToken": lease["fencingToken"], "leaseSeconds": 3600},
        headers=auth(boot["executor"]),
    )
    assert beat.status_code == 200, beat.text
    extended = datetime.fromisoformat(beat.json()["leaseExpiresAt"])
    # Claimed with 5 + 30 s; the deadline is attempt start + 5 * 2 + 30 s,
    # so the hour asked for becomes at most five more seconds.
    assert extended >= initial
    assert (extended - initial).total_seconds() <= 6


async def test_executor_session_holds_the_lease(
    client: httpx.AsyncClient, boot: dict[str, Any]
) -> None:
    await published(client, boot["admin"])
    created = (await invoke(client, boot["caller"], "repo.search", {"query": "x"})).json()
    work_session = await open_session(client, boot["executor"])
    claimed = await client.post(
        "/api/v1/skill-invocations:claim",
        json={
            "protocols": ["local"],
            "localEntrypoints": ["cp_skills.search:run"],
            "sessionId": work_session["id"],
        },
        headers=auth(boot["executor"]),
    )
    lease = claimed.json()["invocation"]
    assert lease["executorSessionId"] == work_session["id"]
    url = f"/api/v1/skill-invocations/{created['id']}"

    other_session = await open_session(client, boot["executor"])
    for body in (
        {"fencingToken": lease["fencingToken"]},
        {"fencingToken": lease["fencingToken"], "sessionId": other_session["id"]},
    ):
        for action, extra in (
            (":heartbeat", {}),
            (":complete", {"output": {"hits": 1}}),
            (":fail", {"error": {"code": "x"}}),
        ):
            response = await client.post(
                f"{url}{action}", json={**body, **extra}, headers=auth(boot["executor"])
            )
            assert response.status_code == 409, (action, response.text)
            assert response.json()["error"]["code"] == "stale_invocation_lease"

    done = await client.post(
        f"{url}:complete",
        json={
            "fencingToken": lease["fencingToken"],
            "sessionId": work_session["id"],
            "output": {"hits": 1},
        },
        headers=auth(boot["executor"]),
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "succeeded"
