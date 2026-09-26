"""v0.3 execution runtime: checkpoints, run actions/budget, suspension,
approval gates, artifact revisions, skill version resolution, cancellation."""

from typing import Any

import httpx
import pytest

from tests.helpers import (
    ORG_AGENT_PERMISSIONS,
    assign_skill,
    auth,
    backdate_expiry,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    open_session,
    register_skill,
)

pytestmark = pytest.mark.usefixtures("clean_database")


async def _claim_and_run(
    client: httpx.AsyncClient,
    key: str,
    task_id: str,
    *,
    run_extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    session = await open_session(client, key)
    claim = (
        await client.post(
            f"/api/v1/tasks/{task_id}:claim",
            json={"sessionId": session["id"]},
            headers=auth(key),
        )
    ).json()
    response = await client.post(
        f"/api/v1/tasks/{task_id}:start-run",
        json={
            "claimId": claim["id"],
            "fencingToken": claim["fencingToken"],
            **(run_extra or {}),
        },
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return session, claim, response.json()


# --- checkpoints --------------------------------------------------------------


async def test_checkpoint_lifecycle(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])

    response = await client.post(
        f"/api/v1/runs/{run['id']}/checkpoints",
        json={"kind": "working_state", "data": {"branch": "fix/483", "nextStep": "tests"}},
        headers=auth(agent_key),
    )
    assert response.status_code == 201, response.text
    assert response.json()["seq"] == 1

    response = await client.post(
        f"/api/v1/runs/{run['id']}/checkpoints",
        json={"kind": "working_state", "data": {"nextStep": "docs"}},
        headers=auth(agent_key),
    )
    assert response.json()["seq"] == 2

    listed = (
        await client.get(f"/api/v1/runs/{run['id']}/checkpoints", headers=auth(agent_key))
    ).json()["items"]
    assert [c["seq"] for c in listed] == [1, 2]
    assert listed[1]["data"] == {"nextStep": "docs"}


async def test_checkpoint_requires_live_ownership(client: httpx.AsyncClient, sync_engine) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, thief_key = await create_agent_with_key(client, admin_key, name="thief")
    task = await create_task(client, admin_key)
    _, claim, run = await _claim_and_run(client, agent_key, task["id"])

    # A different principal cannot checkpoint someone else's run.
    response = await client.post(
        f"/api/v1/runs/{run['id']}/checkpoints",
        json={"kind": "x"},
        headers=auth(thief_key),
    )
    assert response.status_code == 403

    # After takeover the original owner's checkpoint writes are fenced off.
    backdate_expiry(sync_engine, "task_claims", claim["id"])
    thief_session = await open_session(client, thief_key)
    response = await client.post(
        f"/api/v1/tasks/{task['id']}:claim",
        json={"sessionId": thief_session["id"]},
        headers=auth(thief_key),
    )
    assert response.status_code == 200
    response = await client.post(
        f"/api/v1/runs/{run['id']}/checkpoints",
        json={"kind": "x"},
        headers=auth(agent_key),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] in ("stale_claim", "run_not_active")


async def test_checkpoint_idempotency_replay(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])

    headers = {**auth(agent_key), "Idempotency-Key": "cp-1"}
    first = await client.post(
        f"/api/v1/runs/{run['id']}/checkpoints", json={"kind": "s"}, headers=headers
    )
    replay = await client.post(
        f"/api/v1/runs/{run['id']}/checkpoints", json={"kind": "s"}, headers=headers
    )
    assert first.status_code == replay.status_code == 201
    assert first.json()["id"] == replay.json()["id"]
    assert replay.headers.get("Idempotency-Replayed") == "true"


@pytest.mark.parametrize("log", ["checkpoints", "actions"])
async def test_run_log_accepts_limit_and_pages_by_cursor(
    client: httpx.AsyncClient, log: str
) -> None:
    # The console polls these logs with ?limit=; the strict query check
    # (ADR-0058) must not refuse it, and the limit must be honoured.
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])
    body = {"kind": "s"} if log == "checkpoints" else {"action": "tool.call"}
    for _ in range(3):
        response = await client.post(
            f"/api/v1/runs/{run['id']}/{log}", json=body, headers=auth(agent_key)
        )
        assert response.status_code == 201, response.text

    url = f"/api/v1/runs/{run['id']}/{log}"
    first = await client.get(url, params={"limit": 2}, headers=auth(agent_key))
    assert first.status_code == 200, first.text
    assert [item["seq"] for item in first.json()["items"]] == [1, 2]
    cursor = first.json()["nextCursor"]
    assert cursor

    second = await client.get(url, params={"limit": 2, "cursor": cursor}, headers=auth(agent_key))
    assert second.status_code == 200, second.text
    assert [item["seq"] for item in second.json()["items"]] == [3]
    assert second.json()["nextCursor"] is None

    too_many = await client.get(url, params={"limit": 201}, headers=auth(agent_key))
    assert too_many.status_code == 422
    assert too_many.json()["error"]["code"] == "invalid_limit"

    other_task = await create_task(client, admin_key, title="other")
    _, _, other_run = await _claim_and_run(client, agent_key, other_task["id"])
    foreign = await client.get(
        f"/api/v1/runs/{other_run['id']}/{log}", params={"cursor": cursor}, headers=auth(agent_key)
    )
    assert foreign.status_code == 422
    assert foreign.json()["error"]["code"] == "invalid_cursor"


@pytest.mark.parametrize("log", ["checkpoints", "actions"])
async def test_run_log_without_limit_returns_every_entry(
    client: httpx.AsyncClient, log: str
) -> None:
    # The console and the harness adapters read these logs without a cursor
    # (the adapters take the latest checkpoint from the tail), so a request
    # without limit/cursor must keep returning the whole log, not the default
    # page of 50.
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])
    url = f"/api/v1/runs/{run['id']}/{log}"
    body = {"kind": "s"} if log == "checkpoints" else {"action": "tool.call"}
    total = 55
    for _ in range(total):
        response = await client.post(url, json=body, headers=auth(agent_key))
        assert response.status_code == 201, response.text

    everything = await client.get(url, headers=auth(agent_key))
    assert everything.status_code == 200, everything.text
    assert [item["seq"] for item in everything.json()["items"]] == list(range(1, total + 1))
    assert everything.json()["nextCursor"] is None

    page = await client.get(url, params={"limit": 50}, headers=auth(agent_key))
    assert page.status_code == 200, page.text
    assert [item["seq"] for item in page.json()["items"]] == list(range(1, 51))
    cursor = page.json()["nextCursor"]
    assert cursor

    # A cursor alone asks for paging too: the default page size applies.
    rest = await client.get(url, params={"cursor": cursor}, headers=auth(agent_key))
    assert rest.status_code == 200, rest.text
    assert [item["seq"] for item in rest.json()["items"]] == list(range(51, total + 1))
    assert rest.json()["nextCursor"] is None

    unknown = await client.get(url, params={"limt": 10}, headers=auth(agent_key))
    assert unknown.status_code == 400, unknown.text
    assert unknown.json()["error"]["code"] == "invalid_request"
    assert [e["loc"] for e in unknown.json()["error"]["details"]["errors"]] == ["query.limt"]


# --- run actions & budget -----------------------------------------------------


async def test_run_action_recording_and_budget(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"], run_extra={"maxActions": 2})
    assert run["maxActions"] == 2

    for i in range(2):
        response = await client.post(
            f"/api/v1/runs/{run['id']}/actions",
            json={"action": f"tool.call.{i}", "externalReference": f"ref-{i}"},
            headers=auth(agent_key),
        )
        assert response.status_code == 201, response.text

    response = await client.post(
        f"/api/v1/runs/{run['id']}/actions",
        json={"action": "tool.call.3"},
        headers=auth(agent_key),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "budget_exceeded"

    listed = (
        await client.get(f"/api/v1/runs/{run['id']}/actions", headers=auth(agent_key))
    ).json()["items"]
    assert [a["seq"] for a in listed] == [1, 2]
    assert listed[0]["status"] == "completed"


async def test_run_action_two_phase(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])

    started = (
        await client.post(
            f"/api/v1/runs/{run['id']}/actions",
            json={"action": "long.tool", "status": "started"},
            headers=auth(agent_key),
        )
    ).json()
    assert started["finishedAt"] is None

    response = await client.post(
        f"/api/v1/runs/{run['id']}/actions/{started['id']}:finish",
        json={"status": "completed", "externalReference": "pr#42"},
        headers=auth(agent_key),
    )
    assert response.status_code == 200
    finished = response.json()
    assert finished["status"] == "completed"
    assert finished["finishedAt"] is not None
    assert finished["externalReference"] == "pr#42"

    # Double-finish conflicts.
    response = await client.post(
        f"/api/v1/runs/{run['id']}/actions/{started['id']}:finish",
        json={"status": "failed"},
        headers=auth(agent_key),
    )
    assert response.status_code == 409


async def test_run_action_skill_resolution(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=ORG_AGENT_PERMISSIONS
    )
    skill = await register_skill(client, admin_key, "github.create_pr", protocol="mcp")
    await assign_skill(client, admin_key, agent["id"], skill["id"])
    disabled = await register_skill(client, admin_key, "old.tool", protocol="http", version="0.1.0")
    await client.patch(
        f"/api/v1/skills/{disabled['id']}",
        json={"status": "disabled"},
        headers={**auth(admin_key), "If-Match": '"skill-1"'},
    )

    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])

    ok = await client.post(
        f"/api/v1/runs/{run['id']}/actions",
        json={"action": "create pr", "skill": "github.create_pr"},
        headers=auth(agent_key),
    )
    assert ok.status_code == 201
    assert ok.json()["skillId"] == skill["id"]

    # v0.7 (HRS-3): disabled, unassigned and unknown are one answer — the
    # action gate re-derives the effective tool policy and refuses everything
    # outside it without saying which case applied.
    bad = await client.post(
        f"/api/v1/runs/{run['id']}/actions",
        json={"action": "old", "skill": "old.tool"},
        headers=auth(agent_key),
    )
    assert bad.status_code == 403
    assert bad.json()["error"]["code"] == "tool_not_authorized"

    unknown = await client.post(
        f"/api/v1/runs/{run['id']}/actions",
        json={"action": "ghost", "skill": "no.such.tool"},
        headers=auth(agent_key),
    )
    assert unknown.status_code == 403
    assert unknown.json()["error"]["code"] == "tool_not_authorized"


# --- suspension / approval gate ----------------------------------------------


async def test_suspend_resume_cycle_with_approval_gate(client: httpx.AsyncClient) -> None:
    """Scenario D: artifact -> gate approval -> suspend -> approve -> continue."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    admin_id = boot["adminPrincipal"]["id"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, permissions=[*ORG_AGENT_PERMISSIONS, "approvals.manage"]
    )
    task = await create_task(client, admin_key, title="Needs review")
    _, claim, run = await _claim_and_run(client, agent_key, task["id"])

    artifact = (
        await client.post(
            "/api/v1/artifacts",
            json={"type": "document", "name": "draft", "task": task["id"], "runId": run["id"]},
            headers=auth(agent_key),
        )
    ).json()
    approval = (
        await client.post(
            "/api/v1/approvals",
            json={
                "task": task["id"],
                "artifactId": artifact["id"],
                "assignedPrincipalId": admin_id,
                "gate": True,
            },
            headers=auth(agent_key),
        )
    ).json()

    # Checkpoint then suspend: run -> suspended, claim released, task -> todo.
    await client.post(
        f"/api/v1/runs/{run['id']}/checkpoints",
        json={"kind": "working_state", "data": {"draft": artifact["id"]}},
        headers=auth(agent_key),
    )
    response = await client.post(
        f"/api/v1/runs/{run['id']}:suspend",
        json={"waitingForApprovalId": approval["id"]},
        headers=auth(agent_key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run"]["status"] == "suspended"
    assert body["task"]["status"] == "todo"
    assert body["task"]["activeClaimId"] is None

    # While the gate is pending nobody can claim or complete the task.
    session2 = await open_session(client, agent_key)
    blocked = await client.post(
        f"/api/v1/tasks/{task['id']}:claim",
        json={"sessionId": session2["id"]},
        headers=auth(agent_key),
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "approval_required"

    # Approve -> the gate opens; a NEW claim + run continue from checkpoints.
    await client.post(f"/api/v1/approvals/{approval['id']}:approve", headers=auth(admin_key))
    claim2 = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session2["id"]},
            headers=auth(agent_key),
        )
    ).json()
    assert claim2["fencingToken"] == claim["fencingToken"] + 1
    run2 = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:start-run",
            json={"claimId": claim2["id"], "fencingToken": claim2["fencingToken"]},
            headers=auth(agent_key),
        )
    ).json()

    context = (
        await client.get(f"/api/v1/runs/{run2['id']}/context", headers=auth(agent_key))
    ).json()
    assert context["task"]["id"] == task["id"]
    assert [c["kind"] for c in context["checkpoints"]] == ["working_state"]
    assert context["checkpoints"][0]["runId"] == run["id"]
    assert context["artifacts"][0]["id"] == artifact["id"]

    # Finish for real this time.
    response = await client.post(f"/api/v1/runs/{run2['id']}:succeed", headers=auth(agent_key))
    assert response.status_code == 200
    assert response.json()["task"]["status"] == "done"


async def test_rejected_gate_leaves_task_actionable(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    admin_id = boot["adminPrincipal"]["id"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    approval = (
        await client.post(
            "/api/v1/approvals",
            json={"task": task["id"], "assignedPrincipalId": admin_id, "gate": True},
            headers=auth(admin_key),
        )
    ).json()
    await client.post(f"/api/v1/approvals/{approval['id']}:reject", headers=auth(admin_key))

    # Documented semantics: rejection opens the gate; the task stays actionable.
    session = await open_session(client, agent_key)
    response = await client.post(
        f"/api/v1/tasks/{task['id']}:claim",
        json={"sessionId": session["id"]},
        headers=auth(agent_key),
    )
    assert response.status_code == 200


async def test_suspend_requires_live_claim(client: httpx.AsyncClient, sync_engine) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, claim, run = await _claim_and_run(client, agent_key, task["id"])
    backdate_expiry(sync_engine, "task_claims", claim["id"])

    response = await client.post(f"/api/v1/runs/{run['id']}:suspend", headers=auth(agent_key))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_claim"


async def test_gate_requires_task(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    response = await client.post(
        "/api/v1/approvals",
        json={"assignedPrincipalId": boot["adminPrincipal"]["id"], "gate": True},
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_approval"


# --- artifact revisions -------------------------------------------------------


async def test_artifact_revision_lineage(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key, permissions=ORG_AGENT_PERMISSIONS)
    task = await create_task(client, admin_key)

    v1 = (
        await client.post(
            "/api/v1/artifacts",
            json={"type": "document", "name": "draft v1", "task": task["id"]},
            headers=auth(agent_key),
        )
    ).json()
    v2 = (
        await client.post(
            "/api/v1/artifacts",
            json={
                "type": "document",
                "name": "draft v2",
                "task": task["id"],
                "supersedesArtifactId": v1["id"],
            },
            headers=auth(agent_key),
        )
    ).json()
    assert v2["supersedesArtifactId"] == v1["id"]

    fetched = (await client.get(f"/api/v1/artifacts/{v2['id']}", headers=auth(agent_key))).json()
    assert fetched["supersedesArtifactId"] == v1["id"]


async def test_artifact_supersedes_cross_tenant_rejected(
    client: httpx.AsyncClient, sync_engine
) -> None:
    from tests.helpers import make_tenant_directly

    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key, permissions=ORG_AGENT_PERMISSIONS)
    task = await create_task(client, admin_key)
    mine = (
        await client.post(
            "/api/v1/artifacts",
            json={"type": "f", "name": "mine", "task": task["id"]},
            headers=auth(agent_key),
        )
    ).json()

    _, other_key = make_tenant_directly(sync_engine, "rival")
    response = await client.post(
        "/api/v1/artifacts",
        json={"type": "f", "name": "steal", "supersedesArtifactId": mine["id"]},
        headers=auth(other_key),
    )
    assert response.status_code == 404


# --- skill version requirements ----------------------------------------------


async def test_exact_skill_version_requirement(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=ORG_AGENT_PERMISSIONS
    )
    v1 = await register_skill(client, admin_key, "deploy", protocol="http", version="1.0.0")
    v2 = await register_skill(client, admin_key, "deploy", protocol="http", version="2.0.0")
    await assign_skill(client, admin_key, agent["id"], v1["id"])

    pinned = await create_task(
        client, admin_key, title="Pinned", requirements={"skills": ["deploy@2.0.0"]}
    )
    named = await create_task(client, admin_key, title="Named", requirements={"skills": ["deploy"]})

    session = await open_session(client, agent_key)
    # By-name requirement: any assigned version qualifies (v0.2 semantics kept).
    response = await client.post(
        f"/api/v1/tasks/{named['id']}:claim",
        json={"sessionId": session["id"]},
        headers=auth(agent_key),
    )
    assert response.status_code == 200, response.text

    # Pinned requirement: v1 assignment does not satisfy deploy@2.0.0.
    response = await client.post(
        f"/api/v1/tasks/{pinned['id']}:claim",
        json={"sessionId": session["id"]},
        headers=auth(agent_key),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_eligible"
    assert response.json()["error"]["details"]["missingSkills"] == ["deploy@2.0.0"]

    await assign_skill(client, admin_key, agent["id"], v2["id"])
    response = await client.post(
        f"/api/v1/tasks/{pinned['id']}:claim",
        json={"sessionId": session["id"]},
        headers=auth(agent_key),
    )
    assert response.status_code == 200


async def test_unknown_pinned_version_rejected_at_definition(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    await register_skill(client, admin_key, "deploy", protocol="http", version="1.0.0")
    response = await client.post(
        "/api/v1/tasks",
        json={"title": "x", "requirements": {"skills": ["deploy@9.9.9"]}},
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_requirement"


# --- cancellation -------------------------------------------------------------


async def test_cancel_request_signal_and_finalize(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])

    # Any tasks.write principal can request; the harness observes and stops.
    response = await client.post(
        f"/api/v1/runs/{run['id']}:request-cancel",
        json={"reason": "human changed their mind"},
        headers=auth(admin_key),
    )
    assert response.status_code == 200
    assert response.json()["cancelRequestedAt"] is not None

    # Idempotent: repeating the signal does not error or duplicate events.
    again = await client.post(f"/api/v1/runs/{run['id']}:request-cancel", headers=auth(admin_key))
    assert again.status_code == 200

    events = (
        await client.get(
            "/api/v1/events",
            params={"entityType": "run", "entityId": run["id"]},
            headers=auth(agent_key),
        )
    ).json()["items"]
    cancel_events = [e for e in events if e["type"] == "run.cancel_requested"]
    assert len(cancel_events) == 1

    # The harness finalizes; the run can no longer succeed afterwards.
    response = await client.post(f"/api/v1/runs/{run['id']}:cancel", headers=auth(agent_key))
    assert response.status_code == 200
    response = await client.post(f"/api/v1/runs/{run['id']}:succeed", headers=auth(agent_key))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "run_not_active"
