"""v0.3 Harness Protocol: session registration, negotiation, bootstrap context."""

import uuid

import httpx
import pytest

from tests.helpers import (
    ORG_AGENT_PERMISSIONS,
    assign_role,
    auth,
    create_agent_with_key,
    create_role,
    create_task,
    do_bootstrap,
    make_tenant_directly,
    open_session,
)

pytestmark = pytest.mark.usefixtures("clean_database")

HARNESS = {
    "type": "claude-code",
    "version": "2.1.0",
    "protocolVersion": "1",
    "capabilities": [
        "events.realtime",
        "tasks.interactive",
        "artifacts.publish",
        "resume",
        "checkpoints",
        "skills.protocol.mcp",
        "definitely-not-a-capability",
    ],
    "hostname": "devbox.local",
    "environment": {"cwd": "/repo", "repository": "git@github.com:acme/x.git"},
}


async def test_session_open_registers_harness(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)

    session = await open_session(client, agent_key, harness=HARNESS)
    assert session["harnessType"] == "claude-code"
    assert session["harnessVersion"] == "2.1.0"
    assert session["protocolVersion"] == "1"
    assert session["hostname"] == "devbox.local"
    assert session["environment"]["cwd"] == "/repo"
    # Unknown capabilities are silently dropped (forward compatibility).
    assert "definitely-not-a-capability" not in session["harnessCapabilities"]
    assert "skills.protocol.mcp" in session["harnessCapabilities"]


async def test_unsupported_protocol_version_rejected(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    response = await client.post(
        "/api/v1/sessions",
        json={"clientName": "x", "harness": {"type": "claude-code", "protocolVersion": "99"}},
        headers=auth(agent_key),
    )
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "unsupported_protocol_version"
    assert body["details"]["supported"] == ["1", "2"]


async def test_legacy_session_without_harness_still_works(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    session = await open_session(client, agent_key)
    assert session["harnessType"] is None
    assert session["protocolVersion"] is None


async def test_harness_context_bootstrap(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=ORG_AGENT_PERMISSIONS
    )
    role = await create_role(client, admin_key, "engineer")
    await assign_role(client, admin_key, agent["id"], role["id"])

    session = await open_session(client, agent_key, harness=HARNESS)
    task = await create_task(client, admin_key, title="Ship it")
    claim = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session["id"]},
            headers=auth(agent_key),
        )
    ).json()
    run = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:start-run",
            json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
            headers=auth(agent_key),
        )
    ).json()

    response = await client.get("/api/v1/harness/context", headers=auth(agent_key))
    assert response.status_code == 200
    context = response.json()

    assert context["protocol"]["supportedVersions"] == ["1", "2"]
    assert context["protocol"]["name"] == "control-harness"
    assert context["principal"]["id"] == agent["id"]
    assert context["tenant"]["slug"] == "acme"
    assert [s["id"] for s in context["activeSessions"]] == [session["id"]]
    assert context["activeClaims"][0]["taskId"] == task["id"]
    assert context["activeClaims"][0]["fencingToken"] == claim["fencingToken"]
    assert [r["id"] for r in context["activeRuns"]] == [run["id"]]
    assert [r["slug"] for r in context["roles"]] == ["engineer"]
    assert context["eventCursor"].startswith("ec1_")
    assert "tasks.claim" in context["permissions"]

    # Scoped to a specific session
    response = await client.get(
        f"/api/v1/harness/context?sessionId={session['id']}", headers=auth(agent_key)
    )
    assert response.json()["session"]["harnessType"] == "claude-code"

    # A foreign session id is a 404, not someone else's data
    response = await client.get(
        f"/api/v1/harness/context?sessionId={uuid.uuid4()}", headers=auth(agent_key)
    )
    assert response.status_code == 404


async def test_harness_context_needs_only_authentication(client: httpx.AsyncClient) -> None:
    """Context is self-describing: works with a minimal-permission key."""
    boot = await do_bootstrap(client)
    _, key = await create_agent_with_key(
        client, boot["apiKey"]["key"], permissions=["sessions.open"]
    )
    response = await client.get("/api/v1/harness/context", headers=auth(key))
    assert response.status_code == 200
    assert response.json()["activeClaims"] == []


async def test_harness_context_tenant_isolation(client: httpx.AsyncClient, sync_engine) -> None:
    await do_bootstrap(client)
    _, other_key = make_tenant_directly(sync_engine, "other")
    response = await client.get("/api/v1/harness/context", headers=auth(other_key))
    assert response.status_code == 200
    assert response.json()["tenant"]["slug"] == "other"
    assert response.json()["activeSessions"] == []
