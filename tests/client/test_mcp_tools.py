"""Control Plane MCP tools against a real test Control Plane.

The MCP server is a stateless adapter over the SDK; these tests exercise the
tool functions directly (the stdio framing belongs to the MCP SDK) with the
process-local STATE wired to an ASGI-backed client — the same code path
Claude Code drives.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

import control_plane_mcp.server as mcp_server
from control_plane_client import ControlPlaneClient
from tests.helpers import (
    ORG_AGENT_PERMISSIONS,
    assign_skill,
    auth,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    register_skill,
)
from tests.integration.test_task_types_v08 import QUESTION_LIFECYCLE


@pytest.fixture(autouse=True)
async def _fresh_state(app) -> AsyncIterator[None]:
    """Reset MCP process-local state around every test."""
    state = mcp_server.STATE
    state.client = None
    state.session_id = None
    state.claim_id = None
    state.fencing_token = None
    state.run_id = None
    state.task_ref = None
    state.heartbeats = None  # a previous test's runner belongs to a dead loop
    state.session_lock = None
    yield
    if state.client is not None:
        await state.client.aclose()
        state.client = None


def _wire(app, api_key: str) -> None:
    mcp_server.STATE.client = ControlPlaneClient(
        "http://testserver", api_key, transport=httpx.ASGITransport(app=app)
    )


def _load(payload: str) -> Any:
    return json.loads(payload)


async def test_concurrent_session_initialization_opens_once(app) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.open_calls = 0
            self.heartbeat_calls = 0

        async def open_session(self, **_: Any) -> dict[str, str]:
            self.open_calls += 1
            await asyncio.sleep(0.01)
            return {"id": "shared-session"}

        async def heartbeat_session(self, _: str) -> None:
            self.heartbeat_calls += 1

        async def aclose(self) -> None:
            return None

    fake = FakeClient()
    mcp_server.STATE.client = fake  # type: ignore[assignment]

    sessions = await asyncio.gather(
        mcp_server._ensure_session(),
        mcp_server._ensure_session(),
    )

    assert sessions == ["shared-session", "shared-session"]
    assert fake.open_calls == 1
    assert fake.heartbeat_calls == 1


async def test_full_human_flow(client: httpx.AsyncClient, app) -> None:
    """whoami -> list work -> inspect -> claim -> run -> artifact -> complete."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, human_key = await create_agent_with_key(
        client, admin_key, name="human", permissions=ORG_AGENT_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="Fix regression #483")
    _wire(app, human_key)

    who = _load(await mcp_server.cp_whoami())
    assert who["tenant"]["slug"] == "acme"

    work = _load(await mcp_server.cp_list_work())
    assert task["id"] in [t["id"] for t in work["items"]]

    inspected = _load(await mcp_server.cp_get_task(task["id"]))
    assert inspected["claimability"]["claimable"] is True

    claim = _load(await mcp_server.cp_claim_task(task["id"], intent="user picked it"))
    assert claim["fencingToken"] == 1

    run = _load(await mcp_server.cp_start_run())
    assert run["status"] == "running"

    context = _load(await mcp_server.cp_get_run_context())
    assert context["task"]["id"] == task["id"]

    await mcp_server.cp_checkpoint("working_state", {"branch": "fix/483"})
    await mcp_server.cp_record_action("git.commit", external_reference="abc123")
    artifact = _load(
        await mcp_server.cp_create_artifact("git.commit", "Fix stale lease race", uri="git:abc123")
    )
    assert artifact["taskId"] == task["id"]
    assert artifact["runId"] == run["id"]

    result = _load(await mcp_server.cp_complete_run(output={"pr": 484}))
    assert result["task"]["status"] == "done"
    assert result["run"]["status"] == "succeeded"

    # Server-side invariants after the flow (Definition of Done, scenario A).
    events = (
        await client.get(
            "/api/v1/events",
            params={"entityType": "task", "entityId": task["id"]},
            headers=auth(admin_key),
        )
    ).json()["items"]
    types = [e["type"] for e in events]
    assert types[0] == "task.created" and types[-1] == "task.completed"
    assert "task.claimed" in types


async def test_active_turn_control_mcp(client: httpx.AsyncClient, app) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, human_key = await create_agent_with_key(
        client, admin_key, name="controller", permissions=ORG_AGENT_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="Controlled task")
    _wire(app, human_key)

    _load(await mcp_server.cp_claim_task(task["id"], intent="user selected"))
    run = _load(await mcp_server.cp_start_run())
    created = _load(
        await mcp_server.cp_control_run(
            "steer",
            "turn:1",
            run["version"],
            directive="Apply the correction",
        )
    )
    listed = _load(await mcp_server.cp_list_run_controls())
    assert listed["items"][0]["id"] == created["controlMessage"]["id"]
    applied = _load(
        await mcp_server.cp_ack_run_control(
            created["controlMessage"]["id"],
            "applied",
            created["runVersion"],
            1,
            safe_boundary="tool-batch:1:complete",
        )
    )
    assert applied["controlMessage"]["status"] == "applied"


async def test_approval_flow_with_suspension(client: httpx.AsyncClient, app) -> None:
    """Scenario D through MCP: artifact -> gate approval -> suspend ->
    another principal approves -> events show it -> work resumes."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    admin_id = boot["adminPrincipal"]["id"]
    _, human_key = await create_agent_with_key(
        client,
        admin_key,
        name="author",
        permissions=[*ORG_AGENT_PERMISSIONS, "approvals.manage", "approvals.read"],
    )
    task = await create_task(client, admin_key, title="Draft the report")
    _wire(app, human_key)

    await mcp_server.cp_claim_task(task["id"])
    await mcp_server.cp_start_run()
    draft = _load(await mcp_server.cp_create_artifact("document", "draft v1"))

    cursor = _load(await mcp_server.cp_context())["eventCursor"]
    approval = _load(
        await mcp_server.cp_request_approval(
            comment="please review",
            gate=True,
            assigned_principal_id=admin_id,
            artifact_id=draft["id"],
        )
    )
    assert approval["gate"] is True

    suspended = _load(await mcp_server.cp_suspend_run(waiting_for_approval_id=approval["id"]))
    assert suspended["run"]["status"] == "suspended"

    # While pending, the gate blocks re-claiming.
    blocked = _load(await mcp_server.cp_claim_task(task["id"]))
    assert blocked["error"] == "approval_required"

    # The approver (another principal) decides via plain HTTP.
    response = await client.post(
        f"/api/v1/approvals/{approval['id']}:approve",
        json={"comment": "ship it"},
        headers=auth(admin_key),
    )
    assert response.status_code == 200

    # The harness observes the decision through event replay...
    events = _load(await mcp_server.cp_list_events(after=cursor))
    types = [e["type"] for e in events["items"]]
    assert "approval.requested" in types
    assert "approval.approved" in types

    # ...and safely continues: new claim, new run, checkpoint context intact.
    claim2 = _load(await mcp_server.cp_claim_task(task["id"]))
    assert claim2["fencingToken"] == 2
    run2 = _load(await mcp_server.cp_start_run())
    context = _load(await mcp_server.cp_get_run_context(run2["id"]))
    assert [a["id"] for a in context["artifacts"]] == [draft["id"]]
    result = _load(await mcp_server.cp_complete_run())
    assert result["task"]["status"] == "done"


async def test_restart_recovery_via_context(client: httpx.AsyncClient, app) -> None:
    """Scenario B through MCP: a fresh process finds its work in context."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, human_key = await create_agent_with_key(client, admin_key, permissions=ORG_AGENT_PERMISSIONS)
    task = await create_task(client, admin_key, title="Interrupted work")
    _wire(app, human_key)
    await mcp_server.cp_claim_task(task["id"])
    run = _load(await mcp_server.cp_start_run())

    # "Claude Code restart": all process-local state is gone.
    old_client = mcp_server.STATE.client
    mcp_server.STATE.__dict__.clear()
    mcp_server.STATE.client = old_client

    context = _load(await mcp_server.cp_context())
    assert context["activeClaims"][0]["taskId"] == task["id"]
    assert context["activeRuns"][0]["id"] == run["id"]
    assert context["local"]["claimId"] is None  # local cache empty, server truth wins


async def test_reclaiming_held_task_reuses_the_live_claim(client: httpx.AsyncClient, app) -> None:
    """A retried claim (ambiguous failure, or the user asking twice) must not
    bump the fencing epoch and orphan our own in-flight run."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, human_key = await create_agent_with_key(client, admin_key, permissions=ORG_AGENT_PERMISSIONS)
    task = await create_task(client, admin_key)
    _wire(app, human_key)

    first = _load(await mcp_server.cp_claim_task(task["id"]))
    run = _load(await mcp_server.cp_start_run())

    second = _load(await mcp_server.cp_claim_task(task["id"]))
    assert second["reused"] is True
    assert second["fencingToken"] == first["fencingToken"]
    assert mcp_server.STATE.run_id == run["id"]  # our run is still current

    result = _load(await mcp_server.cp_complete_run())
    assert result["task"]["status"] == "done"


async def test_stale_claim_reported_with_hint(client: httpx.AsyncClient, app, sync_engine) -> None:
    from tests.helpers import backdate_expiry, open_session

    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, human_key = await create_agent_with_key(client, admin_key, permissions=ORG_AGENT_PERMISSIONS)
    _, rival_key = await create_agent_with_key(client, admin_key, name="rival")
    task = await create_task(client, admin_key)
    _wire(app, human_key)
    claim = _load(await mcp_server.cp_claim_task(task["id"]))
    _load(await mcp_server.cp_start_run())

    backdate_expiry(sync_engine, "task_claims", claim["id"])
    rival_session = await open_session(client, rival_key)
    await client.post(
        f"/api/v1/tasks/{task['id']}:claim",
        json={"sessionId": rival_session["id"]},
        headers=auth(rival_key),
    )

    result = _load(await mcp_server.cp_complete_run())
    assert result["error"] == "stale_claim"
    assert "hint" in result  # the model is told to stop and consult the user


async def test_remember_and_get_context_tools(client: httpx.AsyncClient, app) -> None:
    """cp_remember writes a replayable observation; cp_get_context returns the
    combined operational+memory context through the Control Plane only (no
    Memory credentials on the harness side)."""
    from tests.integration.test_context_api_v04 import SpyProvider

    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, human_key = await create_agent_with_key(
        client, admin_key, name="human", permissions=ORG_AGENT_PERMISSIONS
    )
    task = await create_task(client, admin_key, title="Investigate flaky test")
    _wire(app, human_key)

    await mcp_server.cp_claim_task(task["id"], intent="user picked it")
    remembered = _load(
        await mcp_server.cp_remember(
            "The flake comes from a socket timeout",
            kind="finding",
            assertions=[
                {"assert": "entity", "entity": {"key": "finding:socket", "type": "finding"}}
            ],
        )
    )
    assert remembered["kind"] == "finding"

    # The finding is a journal event tied to the claimed task and session.
    events = (
        await client.get("/api/v1/events", params={"limit": 200}, headers=auth(admin_key))
    ).json()["items"]
    recorded = [e for e in events if e["type"] == "observation.recorded"]
    assert len(recorded) == 1
    assert recorded[0]["payload"]["taskId"] == task["id"]
    assert recorded[0]["sessionId"] is not None
    assert recorded[0]["payload"]["assertions"][0]["entity"]["key"] == "finding:socket"

    spy = SpyProvider()
    app.state.context_provider = spy
    try:
        context = _load(
            await mcp_server.cp_get_context(query="continue", anchors=["finding:socket"])
        )
    finally:
        app.state.context_provider = None
    assert context["memoryStatus"] == "ok"
    assert context["operational"]["focus"]["task"]["id"] == task["id"]
    assert f"task:{task['id']}" in spy.context_requests[0]["scopes"]

    assert "finding:socket" in spy.context_requests[0]["anchors"]


async def test_operator_tools_and_two_harness_processes(
    client: httpx.AsyncClient, app, monkeypatch: pytest.MonkeyPatch
) -> None:
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    _wire(app, key)
    monkeypatch.setenv("CONTROL_PLANE_HARNESS_TYPE", "codex")
    monkeypatch.setenv("CONTROL_PLANE_HARNESS_VERSION", "codex-test")
    monkeypatch.setenv("CONTROL_PLANE_HARNESS_CLIENT_NAME", "codex-operator")

    root = _load(await mcp_server.cp_create_task("Root operator intent"))
    child = _load(
        await mcp_server.cp_create_task(
            "Child implementation", priority="high", parent_task=root["id"]
        )
    )
    listed = _load(await mcp_server.cp_list_tasks(status="todo"))
    assert {item["id"] for item in listed["items"]} == {root["id"], child["id"]}
    updated = _load(
        await mcp_server.cp_update_task(
            child["id"], child["version"], description="Human-confirmed scope"
        )
    )
    assert updated["version"] == child["version"] + 1

    claim1 = _load(await mcp_server.cp_claim_task(child["id"], intent="human selected"))
    run1 = _load(await mcp_server.cp_start_run())
    handoff = _load(
        await mcp_server.cp_prepare_handoff(
            "First harness finished the initial change",
            next_steps=["Read context", "Continue"],
            evidence_refs=["git:first"],
        )
    )
    assert handoff["run"]["status"] == "suspended"
    first_client = mcp_server.STATE.client
    assert first_client is not None
    await first_client.aclose()

    # A second MCP process has no shared local state and registers distinctly.
    mcp_server.STATE = mcp_server._State()
    _wire(app, key)
    monkeypatch.setenv("CONTROL_PLANE_HARNESS_TYPE", "claude-code")
    monkeypatch.setenv("CONTROL_PLANE_HARNESS_VERSION", "claude-test")
    monkeypatch.setenv("CONTROL_PLANE_HARNESS_CLIENT_NAME", "claude-code-operator")
    claim2 = _load(await mcp_server.cp_claim_task(child["id"], intent="continue handoff"))
    run2 = _load(await mcp_server.cp_start_run())
    context = _load(await mcp_server.cp_get_run_context())
    assert claim1["fencingToken"] == 1
    assert claim2["fencingToken"] == 2
    assert run2["id"] != run1["id"]
    assert context["checkpoints"][0]["id"] == handoff["checkpoint"]["id"]

    sessions = (await client.get("/api/v1/sessions", headers=auth(key))).json()["items"]
    assert {session["harnessType"] for session in sessions} == {"codex", "claude-code"}
    assert {session["controlLevel"] for session in sessions} == {"human_operated"}


async def test_scoped_tool_discovery_through_the_bridge(client: httpx.AsyncClient, app) -> None:
    """HRS-3 through MCP: search narrows, describe loads one schema, the bridge
    grants nothing — an unassigned tool is invisible and unusable."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, name="tool-user", permissions=ORG_AGENT_PERMISSIONS
    )
    mine = await register_skill(
        client,
        admin_key,
        "repo.search",
        protocol="mcp",
        description="Search files in the target repository",
        inputSchema={
            "type": "object",
            "properties": {"pattern": {"type": "string", "default": "TODO"}},
        },
    )
    await register_skill(client, admin_key, "payments.refund", protocol="mcp")
    await assign_skill(client, admin_key, agent["id"], mine["id"])

    task = await create_task(client, admin_key, title="Tooling task")
    _wire(app, agent_key)
    _load(await mcp_server.cp_claim_task(task["id"], intent="user selected"))
    run = _load(await mcp_server.cp_start_run())

    found = _load(await mcp_server.cp_search_tools("repository"))
    assert [item["name"] for item in found["items"]] == ["repo.search"]
    assert found["view"]["policyRevision"].startswith("sha256:")

    described = _load(await mcp_server.cp_describe_tool("repo.search"))
    assert described["inputSchema"] == {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
    }
    assert described["schemaRedactions"] == ["properties.pattern.default"]

    assert _load(await mcp_server.cp_search_tools("refund"))["items"] == []
    hidden = _load(await mcp_server.cp_describe_tool("payments.refund"))
    assert hidden["error"] == "not_found"

    recorded = _load(await mcp_server.cp_record_action("search the repo", skill="repo.search"))
    assert recorded["skillId"] == mine["id"]
    denied = _load(await mcp_server.cp_record_action("refund it", skill="payments.refund"))
    assert denied["error"] == "tool_not_authorized"
    assert run["id"] == mcp_server.STATE.run_id


QUESTION_OUTCOMES = {
    "gates": {"default": {"outcomes": {"approved": [{"comment": {"body": "Answer accepted"}}]}}}
}


async def _declare_question_type(client: httpx.AsyncClient, admin_key: str) -> None:
    response = await client.post(
        "/api/v1/task-types",
        json={
            "key": "question",
            "displayName": "Question",
            "lifecycleSchema": QUESTION_LIFECYCLE,
            "approvalSchema": QUESTION_OUTCOMES,
        },
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text


async def test_task_type_registry_is_visible_and_read_only(client: httpx.AsyncClient, app) -> None:
    """An agent must see the tenant's vocabulary before it writes a status."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    await _declare_question_type(client, admin_key)
    _wire(app, admin_key)

    types = _load(await mcp_server.cp_list_task_types(key="question"))
    declared = types["items"][0]
    categories = {s["key"]: s["systemStatusCategory"] for s in declared["statuses"]}
    assert declared["initialStatus"] == "asked"
    assert categories["answered"] == "terminal_success"

    detail = _load(await mcp_server.cp_get_task_type(declared["id"]))
    assert detail["lifecycleSchema"]["completionStatus"] == "answered"
    # What a decided gate approval sets in motion is part of the type (CP-ADR-0061).
    assert detail["approvalSchema"] == QUESTION_OUTCOMES
    assert declared["declaresApprovalOutcomes"] is True

    task = _load(await mcp_server.cp_create_task("Why is the lease lost?", type_key="question"))
    assert (task["typeKey"], task["status"]) == ("question", "asked")

    inspected = _load(await mcp_server.cp_get_task(task["id"]))
    assert inspected["transitions"]["targets"] == [
        {
            "status": "dropped",
            "displayName": "Dropped",
            "systemStatusCategory": "terminal_cancelled",
            "route": "update",
        },
        {
            "status": "investigating",
            "displayName": "Investigating",
            "systemStatusCategory": "active",
            "route": "update",
        },
    ]

    # The harness may read the registry, never reshape it (SPEC §12).
    registry_tools = [name for name in dir(mcp_server) if "task_type" in name]
    assert sorted(registry_tools) == ["cp_get_task_type", "cp_list_task_types"]


async def test_a_status_change_follows_the_advertised_route(client: httpx.AsyncClient, app) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    await _declare_question_type(client, admin_key)
    _wire(app, admin_key)
    task = _load(await mcp_server.cp_create_task("Why is the lease lost?", type_key="question"))

    moved = _load(
        await mcp_server.cp_update_task(task["id"], task["version"], status="investigating")
    )
    transitions = _load(await mcp_server.cp_get_task(task["id"]))["transitions"]
    routes = {t["status"]: t["route"] for t in transitions["targets"]}

    assert moved["status"] == "investigating"
    assert routes["answered"] == "complete"
    refused = _load(
        await mcp_server.cp_update_task(task["id"], moved["version"], status="answered")
    )
    assert refused["error"] == "invalid_status"


# --- custom fields, planned dates and date-ordered reads (ADR-0049) -----------


async def test_fields_and_dates_travel_through_the_mcp_surface(
    client: httpx.AsyncClient, app
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _wire(app, admin_key)

    task = _load(
        await mcp_server.cp_create_task(
            "Ship the registry",
            custom_fields={"component": "control-plane"},
            due_date="2026-09-08T12:00:00Z",
        )
    )
    assert task["customFields"] == {"component": "control-plane"}
    assert task["dueDate"] is not None

    cleared = _load(
        await mcp_server.cp_update_task(task["id"], task["version"], clear_due_date=True)
    )
    assert cleared["dueDate"] is None


async def test_a_date_cannot_be_set_and_cleared_in_one_update(
    client: httpx.AsyncClient, app
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _wire(app, admin_key)
    task = _load(await mcp_server.cp_create_task("Ambiguous"))

    refused = _load(
        await mcp_server.cp_update_task(
            task["id"], task["version"], due_date="2026-09-08T12:00:00Z", clear_due_date=True
        )
    )

    assert refused["error"] == "invalid_request"
    # Nothing was written: the task is still at version 1.
    assert _load(await mcp_server.cp_get_task(task["id"]))["task"]["version"] == 1


async def test_secrets_in_custom_fields_are_refused_at_the_surface(
    client: httpx.AsyncClient, app
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _wire(app, admin_key)

    refused = _load(
        await mcp_server.cp_create_task("Leak", custom_fields={"apiToken": "sk-live-1"})
    )

    assert refused["error"] == "secret_material_rejected"


async def test_tasks_can_be_read_soonest_due_first(client: httpx.AsyncClient, app) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _wire(app, admin_key)
    await mcp_server.cp_create_task("Later", due_date="2026-10-01T12:00:00Z")
    await mcp_server.cp_create_task("Sooner", due_date="2026-09-01T12:00:00Z")
    await mcp_server.cp_create_task("Undated")

    page = _load(await mcp_server.cp_list_tasks(sort="dueDate"))

    assert [t["title"] for t in page["items"]] == ["Sooner", "Later", "Undated"]


async def test_a_thread_can_be_written_and_read_through_the_mcp_surface(
    client: httpx.AsyncClient, app
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _wire(app, admin_key)
    task = _load(await mcp_server.cp_create_task("Discussed"))

    first = _load(await mcp_server.cp_comment(task["id"], "Starting on the migration."))
    assert first["version"] == 1
    await mcp_server.cp_comment(task["id"], "Blocked on the staging database.")

    page = _load(await mcp_server.cp_list_comments(task["id"]))
    assert [c["body"] for c in page["items"]] == [
        "Starting on the migration.",
        "Blocked on the staging database.",
    ]

    edited = _load(
        await mcp_server.cp_edit_comment(
            task["id"], first["id"], "Starting on the migration today.", first["version"]
        )
    )
    assert edited["version"] == 2
    assert edited["editedAt"] is not None


async def test_a_comment_carrying_a_credential_is_refused_at_the_surface(
    client: httpx.AsyncClient, app
) -> None:
    """The agent-facing path is exactly where a pasted token would arrive."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _wire(app, admin_key)
    task = _load(await mcp_server.cp_create_task("Leaky"))

    refused = _load(
        await mcp_server.cp_comment(task["id"], "use sk-live-0123456789abcdefghij to test")
    )

    assert refused["error"] == "secret_material_rejected"
    assert _load(await mcp_server.cp_list_comments(task["id"]))["items"] == []


async def test_only_the_author_may_edit_through_the_mcp_surface(
    client: httpx.AsyncClient, app
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key, name="runner")
    _wire(app, agent_key)
    task = await create_task(client, admin_key, title="Attributed")
    theirs = _load(await mcp_server.cp_comment(task["id"], "Agent's own note"))

    await mcp_server.STATE.client.aclose()
    _wire(app, admin_key)
    refused = _load(
        await mcp_server.cp_edit_comment(task["id"], theirs["id"], "Rewritten", theirs["version"])
    )

    assert refused["error"] == "not_comment_author"


async def test_remember_external_observation_dedups(client: httpx.AsyncClient, app) -> None:
    """cp_remember passes the CP-ADR-0057 fields through; a repeat of the
    same (source, dedup_key) returns the existing observation."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, human_key = await create_agent_with_key(
        client, admin_key, name="human", permissions=ORG_AGENT_PERMISSIONS
    )
    _wire(app, human_key)

    async def remember() -> dict:
        return _load(
            await mcp_server.cp_remember(
                "CI pipeline 981 failed on main",
                kind="external_fact",
                source="gitlab-ci",
                dedup_key="pipeline-981",
                observed_at="2026-09-22T10:00:00+00:00",
                external_ref={"system": "gitlab", "id": "981", "url": "https://example.test/981"},
            )
        )

    first = await remember()
    repeat = await remember()
    assert first["deduplicated"] is False
    assert repeat["deduplicated"] is True
    assert repeat["id"] == first["id"]

    events = (
        await client.get("/api/v1/events", params={"limit": 200}, headers=auth(admin_key))
    ).json()["items"]
    recorded = [e for e in events if e["type"] == "observation.recorded"]
    assert len(recorded) == 1
    payload = recorded[0]["payload"]
    assert payload["source"] == "gitlab-ci"
    assert payload["dedupKey"] == "pipeline-981"
    assert payload["observedAt"] == "2026-09-22T10:00:00+00:00"
    assert payload["externalRef"]["id"] == "981"

    superseding = _load(
        await mcp_server.cp_remember(
            "CI pipeline 981 passed on retry",
            kind="external_fact",
            source="gitlab-ci",
            dedup_key="pipeline-981@retry",
            supersedes=first["id"],
        )
    )
    assert superseding["deduplicated"] is False


# --- goals and the work graph (CP-ADR-0062) ------------------------------------


async def test_goals_and_work_graph_fields_through_the_mcp_surface(
    client: httpx.AsyncClient, app
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _wire(app, admin_key)

    goal = _load(
        await mcp_server.cp_create_goal(
            "Core matches its decisions",
            desired_state="Every accepted decision holds",
            criteria=[{"key": "holds", "kind": "llm_judge", "description": "A judge agrees"}],
        )
    )
    assert goal["status"] == "active"

    refused = _load(
        await mcp_server.cp_create_task("Drift", origin={"kind": "rule", "ruleId": "drift"})
    )
    assert refused["error"] == "invalid_origin"

    task = _load(
        await mcp_server.cp_create_task(
            "Restore the decision",
            goal_id=goal["id"],
            origin={
                "kind": "rule",
                "ruleId": "decision-drift/v1",
                "evidence": [{"kind": "external", "externalRef": {"system": "vcs", "id": "abc"}}],
            },
            acceptance=[{"key": "holds", "kind": "human", "description": "Owner confirms"}],
        )
    )
    assert task["origin"]["ruleId"] == "decision-drift/v1"

    listed = _load(await mcp_server.cp_list_goals(status="active"))
    assert listed["items"][0]["id"] == goal["id"]
    assert listed["items"][0]["criteriaCount"] == 1

    inspected = _load(await mcp_server.cp_get_goal(goal["id"]))
    assert inspected["goal"]["desiredState"] == "Every accepted decision holds"
    assert [(w["id"], w["originKind"]) for w in inspected["work"]["items"]] == [
        (task["id"], "rule")
    ]

    both = _load(
        await mcp_server.cp_update_task(
            task["id"], task["version"], goal_id=goal["id"], clear_goal=True
        )
    )
    assert both["error"] == "invalid_request"
    cleared = _load(await mcp_server.cp_update_task(task["id"], task["version"], clear_goal=True))
    assert cleared["goalId"] is None
    assert _load(await mcp_server.cp_get_goal(goal["id"]))["work"]["items"] == []

    # Closing a goal: an agent or the harness has the tool for it too.
    parent = _load(await mcp_server.cp_create_goal("Parent"))
    ambiguous = _load(
        await mcp_server.cp_update_goal(
            goal["id"], goal["version"], parent_goal_id=parent["id"], clear_parent=True
        )
    )
    assert ambiguous["error"] == "invalid_request"
    stale = _load(await mcp_server.cp_update_goal(goal["id"], goal["version"] + 1, title="x"))
    assert stale["error"] == "version_conflict"
    moved = _load(
        await mcp_server.cp_update_goal(
            goal["id"],
            goal["version"],
            title="Core matches its accepted decisions",
            criteria=[],
            parent_goal_id=parent["id"],
        )
    )
    assert (moved["title"], moved["criteria"], moved["parentGoalId"]) == (
        "Core matches its accepted decisions",
        [],
        parent["id"],
    )
    closed = _load(
        await mcp_server.cp_update_goal(
            goal["id"], moved["version"], status="achieved", clear_parent=True
        )
    )
    assert (closed["status"], closed["parentGoalId"]) == ("achieved", None)
    assert closed["closedAt"] is not None

    # Creating or changing a goal is a decision; reading goals is not.
    withheld = await mcp_server.withheld_tool_names()
    assert "cp_create_goal" in withheld
    assert "cp_update_goal" in withheld
    assert "cp_list_goals" not in withheld
    assert "cp_get_goal" not in withheld


# --- work rules (CP-ADR-0063) ----------------------------------------------------


async def test_rules_are_readable_through_the_mcp_surface(client: httpx.AsyncClient, app) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    created = await client.post(
        "/api/v1/rules",
        json={
            "key": "drift",
            "trigger": {"kind": "observation", "type": "drift.seen"},
            "action": {
                "kind": "ensure_work",
                "taskType": "task",
                "dedupKeyTemplate": "drift:{{payload.data.id}}",
                "fields": {"title": "Drift {{payload.data.id}}"},
            },
        },
        headers=auth(admin_key),
    )
    assert created.status_code == 201, created.text
    rule = created.json()
    _wire(app, admin_key)

    listed = _load(await mcp_server.cp_list_rules(status="enabled"))
    assert listed["items"] == [
        {
            "id": rule["id"],
            "key": "drift",
            "version": 1,
            "status": "enabled",
            "workspaceId": None,
            "goalId": None,
            "trigger": {"kind": "observation", "type": "drift.seen"},
            "skill": None,
            "action": {"kind": "ensure_work", "taskType": "task"},
        }
    ]
    inspected = _load(await mcp_server.cp_get_rule(rule["id"]))
    assert inspected["rule"]["action"]["dedupKeyTemplate"] == "drift:{{payload.data.id}}"
    assert inspected["evaluations"] == {"items": [], "nextCursor": None}
    missing = _load(await mcp_server.cp_get_rule("00000000-0000-0000-0000-000000000000"))
    assert missing["error"] == "not_found"

    # Reading rules is not a decision; writing them stays with the API.
    withheld = await mcp_server.withheld_tool_names()
    assert "cp_list_rules" not in withheld
    assert "cp_get_rule" not in withheld


async def test_recall_tool_reads_the_graph_through_the_control_plane(
    client: httpx.AsyncClient, app
) -> None:
    """cp_recall (CP-ADR-0064): the agent's pull from the knowledge graph goes
    through the Control Plane with the task's workspace namespace, and comes
    back in the same rendering as the task context of the prompt."""
    from tests.fake_graph_memory import FakeGraphMemory
    from tests.helpers import create_workspace

    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "platform")
    _, agent_key = await create_agent_with_key(client, admin_key, name="agent")
    task = await create_task(client, admin_key, title="Touch claims", workspaceId=workspace["id"])
    _wire(app, agent_key)
    mcp_server.STATE.task_ref = task["id"]

    memory = FakeGraphMemory()
    app.state.context_provider = memory
    try:
        plain = _load(
            await mcp_server.cp_recall(
                anchor="POST /tasks/{task_id}:claim", relations=["calls"], direction="in"
            )
        )
        result = _load(
            await mcp_server.cp_recall(
                anchor="POST /tasks/{task_id}:claim",
                relations=["calls"],
                direction="in",
                include_pack=True,
            )
        )
        failed = _load(await mcp_server.cp_recall())
    finally:
        app.state.context_provider = None
    # By default the agent gets the rendering within the budget, not the raw pack.
    assert "pack" not in plain and "### ui_call" in plain["text"]
    keys = {i["natural_key"] for s in result["pack"]["sections"] for i in s["items"]}
    assert "platform-web:src/api/tasks.ts:42" in keys
    assert "### ui_call" in result["text"] and "</recalled_memory>" in result["text"]
    request = memory.typed_requests[0]
    assert f":ws:{workspace['id']}" in request["scope"]["namespaces"][-1]
    # The caller's visibility, narrowed to the task's workspace and the agent:
    # an entity of another workspace in the same namespace stays hidden.
    assert f"workspace:{workspace['id']}" in request["allowedScopes"]
    assert "secret-app:src/api.ts:1" not in keys and "secret-app" not in plain["text"]
    assert failed["error"] == "invalid_recall_request"
