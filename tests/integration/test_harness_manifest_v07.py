"""v0.7 Effective Harness Manifest (HRS-2), end to end.

Covers the verification matrix in
``docs/effective-harness-manifest-threat-model.md``: reproducibility, the
version-on-change rule, immutability, the operational/memory split, ephemeral
markers, provider fallback, the fencing gate and tenant isolation.
"""

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import (
    ORG_AGENT_PERMISSIONS,
    assign_skill,
    auth,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    make_tenant_directly,
    open_session,
    register_skill,
)

pytestmark = pytest.mark.usefixtures("clean_database")


async def _claim_and_run(
    client: httpx.AsyncClient,
    key: str,
    task_id: str,
    *,
    session_extra: dict[str, Any] | None = None,
    run_extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    session = await open_session(client, key, **(session_extra or {}))
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


async def _manifest(client: httpx.AsyncClient, key: str, run_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/runs/{run_id}/harness-manifest", headers=auth(key))
    assert response.status_code == 200, response.text
    return response.json()


# --- compilation at run start -------------------------------------------------


async def test_every_run_gets_a_manifest_at_start(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    session, claim, run = await _claim_and_run(
        client,
        agent_key,
        task["id"],
        session_extra={
            "harness": {
                "type": "claude-code",
                "version": "0.6.0",
                "protocolVersion": "2",
                "capabilities": ["checkpoints", "skills.protocol.mcp"],
            }
        },
        run_extra={"maxActions": 50, "maxDurationSeconds": 900},
    )

    body = await _manifest(client, agent_key, run["id"])
    manifest = body["manifest"]
    assert manifest["version"] == 1
    assert manifest["compileReason"] == "run_started"
    assert manifest["baseHash"].startswith("sha256:")
    assert manifest["supersedesVersion"] is None
    assert body["ephemeral"] == []

    base = manifest["base"]
    assert base["identity"]["harnessType"] == "claude-code"
    assert base["identity"]["sessionId"] == session["id"]
    assert base["run"]["claimId"] == claim["id"]
    assert base["run"]["fencingToken"] == claim["fencingToken"]
    assert base["budgets"] == {
        "maxDurationSeconds": 900,
        "maxActions": 50,
        "governanceCeiling": {},
    }
    # Not-yet-existing subsystems are declared absent, not invented.
    assert base["workerProfile"] == {"status": "unavailable"}
    assert base["executionBackend"] == {"status": "unavailable"}

    # Every base section is attributed.
    assert set(manifest["provenance"]) == set(base) - {"schemaVersion"}
    assert manifest["provenance"]["identity"]["source"] == "server_authoritative"


async def test_operational_and_memory_are_separate_and_unhashed(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])

    first = (await _manifest(client, agent_key, run["id"]))["manifest"]
    assert set(first["captured"]) == {"operational", "memory"}
    assert first["captured"]["operational"]["eventCursor"]
    assert first["captured"]["memory"] is None
    assert "memory" not in first["base"]

    # Recompiling with a memory *reference* changes captured state but not the
    # frozen base: eventually-consistent memory never anchors reproducibility.
    response = await client.post(
        f"/api/v1/runs/{run['id']}/harness-manifest:compile",
        json={"memory": {"packId": "ctx-abc", "provenance": "memory-service", "lagEvents": 2}},
        headers=auth(agent_key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["created"] is False

    again = (await _manifest(client, agent_key, run["id"]))["manifest"]
    assert again["version"] == 1
    assert again["baseHash"] == first["baseHash"]
    # The stored version was NOT rewritten — the no-op returned the active row.
    assert again["captured"]["memory"] is None


# --- versioning ---------------------------------------------------------------


async def test_recompile_without_change_does_not_create_a_version(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])

    response = await client.post(
        f"/api/v1/runs/{run['id']}/harness-manifest:compile", json={}, headers=auth(agent_key)
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "created": False,
        "manifest": {
            **response.json()["manifest"],
            "version": 1,
        },
    }
    versions = (
        await client.get(f"/api/v1/runs/{run['id']}/harness-manifests", headers=auth(agent_key))
    ).json()["items"]
    assert [v["version"] for v in versions] == [1]


async def test_declaring_a_backend_creates_a_new_version(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])
    first = (await _manifest(client, agent_key, run["id"]))["manifest"]

    response = await client.post(
        f"/api/v1/runs/{run['id']}/harness-manifest:compile",
        json={"executionBackend": {"id": "local-process", "capabilityRevision": 2}},
        headers=auth(agent_key),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is True
    assert body["manifest"]["version"] == 2
    assert body["manifest"]["supersedesVersion"] == 1
    assert body["manifest"]["baseHash"] != first["baseHash"]

    # Version 1 is still readable and unchanged: history is not rewritten.
    pinned = (
        await client.get(
            f"/api/v1/runs/{run['id']}/harness-manifest?version=1", headers=auth(agent_key)
        )
    ).json()["manifest"]
    assert pinned["baseHash"] == first["baseHash"]
    assert pinned["base"]["executionBackend"] == {"status": "unavailable"}


async def test_project_governance_change_shows_up_as_a_new_base_hash(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """A configuration change between compilations must be visible, not silent."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])
    first = (await _manifest(client, agent_key, run["id"]))["manifest"]

    # Simulate a budget change on the run itself (the cheapest authoritative
    # input to move without rebuilding the whole project hierarchy).
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE runs SET max_actions = 11 WHERE id = :id"), {"id": run["id"]})

    response = await client.post(
        f"/api/v1/runs/{run['id']}/harness-manifest:compile", json={}, headers=auth(agent_key)
    )
    assert response.status_code == 201, response.text
    assert response.json()["manifest"]["baseHash"] != first["baseHash"]

    latest = (await _manifest(client, agent_key, run["id"]))["manifest"]
    assert latest["base"]["budgets"]["maxActions"] == 11


# --- provider fallback --------------------------------------------------------


async def test_provider_fallback_is_a_new_recorded_attempt(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])

    primary = await client.post(
        f"/api/v1/runs/{run['id']}/harness-manifest:compile",
        json={"model": {"provider": "anthropic", "model": "opus", "attempt": 1}},
        headers=auth(agent_key),
    )
    assert primary.status_code == 201, primary.text
    assert primary.json()["manifest"]["modelAttempt"] == 1

    fallback = await client.post(
        f"/api/v1/runs/{run['id']}/harness-manifest:compile",
        json={
            "reason": "provider_fallback",
            "model": {"provider": "anthropic", "model": "opus", "attempt": 2},
        },
        headers=auth(agent_key),
    )
    assert fallback.status_code == 201, fallback.text
    body = fallback.json()["manifest"]
    assert body["compileReason"] == "provider_fallback"
    assert body["modelAttempt"] == 2
    assert body["supersedesVersion"] == 2

    # A fallback that does not advance the attempt is a silent fallback.
    silent = await client.post(
        f"/api/v1/runs/{run['id']}/harness-manifest:compile",
        json={"reason": "provider_fallback", "model": {"provider": "other", "attempt": 2}},
        headers=auth(agent_key),
    )
    assert silent.status_code == 422
    assert silent.json()["error"]["code"] == "invalid_fallback_attempt"


# --- ephemeral markers --------------------------------------------------------


async def test_ephemeral_marker_never_touches_the_frozen_base(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])
    before = (await _manifest(client, agent_key, run["id"]))["manifest"]

    response = await client.post(
        f"/api/v1/runs/{run['id']}/harness-manifest/ephemeral",
        json={"kind": "budget_warning", "summary": "80% of actions used", "data": {"left": 10}},
        headers=auth(agent_key),
    )
    assert response.status_code == 201, response.text
    assert response.json()["seq"] == 1

    after = await _manifest(client, agent_key, run["id"])
    assert after["manifest"]["baseHash"] == before["baseHash"]
    assert after["manifest"]["snapshotHash"] == before["snapshotHash"]
    assert after["manifest"]["base"] == before["base"]
    assert [e["kind"] for e in after["ephemeral"]] == ["budget_warning"]
    assert "ephemeral" not in after["manifest"]["base"]


async def test_ephemeral_payload_is_guarded(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])

    response = await client.post(
        f"/api/v1/runs/{run['id']}/harness-manifest/ephemeral",
        json={"kind": "steering", "summary": "focus", "data": {"transcript": ["hi"]}},
        headers=auth(agent_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsafe_manifest_payload"


# --- tool visibility ----------------------------------------------------------


async def test_tool_visibility_is_explained(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    principal, agent_key = await create_agent_with_key(
        client, admin_key, permissions=ORG_AGENT_PERMISSIONS
    )
    mcp_skill = await register_skill(client, admin_key, "deploy", protocol="mcp")
    http_skill = await register_skill(client, admin_key, "scrape", protocol="http")
    await assign_skill(client, admin_key, principal["id"], mcp_skill["id"])
    await assign_skill(client, admin_key, principal["id"], http_skill["id"])

    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(
        client,
        agent_key,
        task["id"],
        session_extra={"harness": {"type": "claude-code", "capabilities": ["skills.protocol.mcp"]}},
    )

    manifest = (await _manifest(client, agent_key, run["id"]))["manifest"]
    tools = {t["name"]: t for t in manifest["base"]["toolPolicy"]["tools"]}
    assert tools["deploy"]["visible"] is True
    assert tools["scrape"]["visible"] is False

    visibility = manifest["provenance"]["toolPolicy"]["visibility"]
    assert visibility[mcp_skill["id"]]["reason"] == "assigned_and_protocol_supported"
    assert visibility[http_skill["id"]]["reason"] == "protocol_not_supported_by_harness"


# --- guards, gates, isolation -------------------------------------------------


@pytest.mark.parametrize("section", ["identity", "run", "projectPolicy", "toolPolicy", "budgets"])
async def test_server_authoritative_sections_are_rejected(
    client: httpx.AsyncClient, section: str
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])

    response = await client.post(
        f"/api/v1/runs/{run['id']}/harness-manifest:compile",
        json={section: {"spoofed": True}},
        headers=auth(agent_key),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "server_authoritative_section"


async def test_secret_material_is_rejected(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])

    response = await client.post(
        f"/api/v1/runs/{run['id']}/harness-manifest:compile",
        json={"model": {"provider": "anthropic", "apiKey": "sk-live-123"}},
        headers=auth(agent_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "secret_material_rejected"


async def test_manifest_rows_are_immutable(client: httpx.AsyncClient, sync_engine: Engine) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    await _claim_and_run(client, agent_key, task["id"])

    for statement in (
        "UPDATE run_harness_manifests SET base_hash = 'sha256:' || repeat('0', 64)",
        "DELETE FROM run_harness_manifests",
    ):
        with pytest.raises(Exception, match="immutable"), sync_engine.begin() as conn:
            conn.execute(text(statement))


async def test_compile_requires_a_live_claim(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, other_key = await create_agent_with_key(client, admin_key, name="agent-2")
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])

    # Another principal takes the claim over: the previous run is a zombie.
    from tests.helpers import backdate_expiry

    claim_id = (await client.get(f"/api/v1/runs/{run['id']}", headers=auth(agent_key))).json()[
        "claimId"
    ]
    backdate_expiry(sync_engine, "task_claims", claim_id)
    other_session = await open_session(client, other_key)
    takeover = await client.post(
        f"/api/v1/tasks/{task['id']}:claim",
        json={"sessionId": other_session["id"]},
        headers=auth(other_key),
    )
    assert takeover.status_code in {200, 201}, takeover.text

    response = await client.post(
        f"/api/v1/runs/{run['id']}/harness-manifest:compile", json={}, headers=auth(agent_key)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] in {"stale_claim", "run_not_active"}


async def test_manifest_is_tenant_scoped(client: httpx.AsyncClient, sync_engine: Engine) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])

    _, key_b = make_tenant_directly(sync_engine, "tenant-b")
    response = await client.get(f"/api/v1/runs/{run['id']}/harness-manifest", headers=auth(key_b))
    assert response.status_code == 404


async def test_run_without_manifest_reads_as_404(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Pre-migration runs must degrade predictably, not blow up."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])

    with sync_engine.begin() as conn:
        conn.execute(text("ALTER TABLE run_harness_manifests DISABLE TRIGGER USER"))
        conn.execute(
            text("DELETE FROM run_harness_manifests WHERE run_id = :id"), {"id": run["id"]}
        )
        conn.execute(text("ALTER TABLE run_harness_manifests ENABLE TRIGGER USER"))

    response = await client.get(
        f"/api/v1/runs/{run['id']}/harness-manifest", headers=auth(agent_key)
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_manifest_compilation_emits_events(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])
    await client.post(
        f"/api/v1/runs/{run['id']}/harness-manifest/ephemeral",
        json={"kind": "note", "summary": "checked"},
        headers=auth(agent_key),
    )

    events = (await client.get("/api/v1/events", headers=auth(admin_key))).json()["items"]
    by_type = {e["type"]: e for e in events}
    assert "run.manifest_compiled" in by_type
    assert "run.manifest_ephemeral_recorded" in by_type
    compiled = by_type["run.manifest_compiled"]["payload"]
    assert compiled["baseHash"].startswith("sha256:")
    # The audit trail references evidence; it never copies it.
    assert "base" not in compiled
    assert "provenance" not in compiled
