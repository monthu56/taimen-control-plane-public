"""Durable Child Run Handle HTTP contract (TASK-000007)."""

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import (
    assign_skill,
    auth,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    make_tenant_directly,
    open_session,
    register_skill,
)


def _expire_handle(sync_engine: Engine, handle_id: str) -> None:
    """Move a handle's whole lifetime into the past.

    ``created_at`` moves with ``expires_at`` because the row-level CHECK keeps
    expiry after creation — the invariant is worth more than a shorter helper.
    """
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE run_child_handles "
                "SET created_at = now() - interval '2 hours', "
                "    expires_at = now() - interval '1 hour' "
                "WHERE id = :row_id"
            ),
            {"row_id": handle_id},
        )


async def claim_and_run(
    client: httpx.AsyncClient, agent_key: str, task_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    session = await open_session(client, agent_key)
    claim_response = await client.post(
        f"/api/v1/tasks/{task_id}:claim",
        json={"sessionId": session["id"]},
        headers=auth(agent_key),
    )
    assert claim_response.status_code == 200, claim_response.text
    claim = claim_response.json()
    run = await client.post(
        f"/api/v1/tasks/{task_id}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(agent_key),
    )
    assert run.status_code == 201, run.text
    return claim, run.json()


async def launch(
    client: httpx.AsyncClient,
    key: str,
    run_id: str,
    *,
    correlation_id: str = "review:migration",
    idempotency_key: str = "launch-1",
    **overrides: Any,
) -> httpx.Response:
    body: dict[str, Any] = {
        "correlationId": correlation_id,
        "title": "Verify the migration roundtrip",
    }
    body.update(overrides)
    return await client.post(
        f"/api/v1/runs/{run_id}/child-handles",
        json=body,
        headers={**auth(key), "Idempotency-Key": idempotency_key},
    )


async def test_launch_creates_task_relation_and_handle(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])

    response = await launch(client, agent_key, run["id"], grant={"permissions": ["tasks.read"]})

    assert response.status_code == 201, response.text
    body = response.json()
    handle = body["childHandle"]
    assert response.headers["location"].endswith(f"/child-handles/{handle['id']}")
    assert handle["parentRunId"] == run["id"]
    assert handle["parentTaskId"] == task["id"]
    assert handle["correlationId"] == "review:migration"
    assert handle["childRunId"] is None
    assert handle["status"] == "pending"
    assert handle["depth"] == 1
    assert handle["cancellationPolicy"] == "cascade_cooperative"
    assert handle["grant"]["permissions"] == ["tasks.read"]
    assert handle["result"] is None
    assert body["handleToken"].startswith("ch1_")
    assert body["childTask"]["id"] == handle["childTaskId"]
    assert body["childTask"]["status"] == "todo"

    relations = (
        await client.get(
            f"/api/v1/tasks/{handle['childTaskId']}/relations", headers=auth(agent_key)
        )
    ).json()["items"]
    spawned = [r for r in relations if r["type"] == "spawned_by"]
    assert len(spawned) == 1
    assert spawned[0]["fromTaskId"] == handle["childTaskId"]
    assert spawned[0]["toTaskId"] == task["id"]


async def test_launch_event_carries_references_only(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    handle = (await launch(client, agent_key, run["id"], title="Secret sounding title")).json()[
        "childHandle"
    ]

    events = (
        await client.get(
            "/api/v1/events",
            params={"entityType": "run", "entityId": run["id"]},
            headers=auth(agent_key),
        )
    ).json()["items"]
    launched = [event for event in events if event["type"] == "run.child.launched"]
    assert len(launched) == 1
    payload = launched[0]["payload"]
    assert payload["childHandleId"] == handle["id"]
    assert payload["correlationId"] == "review:migration"
    assert payload["grantSizes"]["permissions"] >= 0
    assert "Secret sounding title" not in str(payload)


async def test_repeat_with_the_same_correlation_id_returns_the_same_child(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])

    first = await launch(client, agent_key, run["id"], idempotency_key="launch-a")
    # A different Idempotency-Key on purpose: a restarted orchestrator keeps
    # its correlation id but generates a fresh request key.
    second = await launch(client, agent_key, run["id"], idempotency_key="launch-b")

    assert first.status_code == 201
    assert second.status_code == 200, second.text
    assert second.json()["childHandle"]["id"] == first.json()["childHandle"]["id"]
    assert second.json()["childTask"]["id"] == first.json()["childTask"]["id"]
    # The one-time secret is not re-issued on replay.
    assert second.json()["handleToken"] is None

    listing = (
        await client.get(f"/api/v1/runs/{run['id']}/child-handles", headers=auth(agent_key))
    ).json()
    assert len(listing["items"]) == 1
    assert listing["hasMore"] is False


async def test_distinct_correlation_ids_create_distinct_children(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])

    await launch(client, agent_key, run["id"], correlation_id="a", idempotency_key="k-a")
    await launch(client, agent_key, run["id"], correlation_id="b", idempotency_key="k-b")

    listing = (
        await client.get(
            f"/api/v1/runs/{run['id']}/child-handles",
            params={"limit": 1},
            headers=auth(agent_key),
        )
    ).json()
    assert len(listing["items"]) == 1
    assert listing["hasMore"] is True
    page_two = (
        await client.get(
            f"/api/v1/runs/{run['id']}/child-handles",
            params={"cursor": listing["nextCursor"]},
            headers=auth(agent_key),
        )
    ).json()
    assert len(page_two["items"]) == 1
    assert page_two["items"][0]["correlationId"] != listing["items"][0]["correlationId"]
    assert page_two["hasMore"] is False


async def test_launch_requires_the_parent_run_holder_and_a_live_claim(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, other_key = await create_agent_with_key(client, admin_key, name="other-agent")
    task = await create_task(client, admin_key)
    claim, run = await claim_and_run(client, agent_key, task["id"])

    foreign = await launch(client, other_key, run["id"], idempotency_key="foreign")
    assert foreign.status_code == 403, foreign.text
    assert foreign.json()["error"]["code"] == "run_holder_mismatch"

    release = await client.post(f"/api/v1/claims/{claim['id']}:release", headers=auth(agent_key))
    assert release.status_code == 200, release.text
    stale = await launch(client, agent_key, run["id"], idempotency_key="stale")
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "stale_claim"


async def test_launch_validates_correlation_id_and_idempotency_header(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])

    bad = await launch(client, agent_key, run["id"], correlation_id="spaces inside")
    assert bad.status_code == 422, bad.text
    assert bad.json()["error"]["code"] == "invalid_correlation_id"

    missing_key = await client.post(
        f"/api/v1/runs/{run['id']}/child-handles",
        json={"correlationId": "c", "title": "t"},
        headers=auth(agent_key),
    )
    assert missing_key.status_code == 422
    assert missing_key.json()["error"]["code"] == "idempotency_key_required"


async def test_child_run_binds_to_the_handle_when_it_starts(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, child_key = await create_agent_with_key(client, admin_key, name="child-agent")
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    handle = (await launch(client, agent_key, run["id"])).json()["childHandle"]

    _, child_run = await claim_and_run(client, child_key, handle["childTaskId"])

    listing = (
        await client.get(f"/api/v1/runs/{run['id']}/child-handles", headers=auth(agent_key))
    ).json()["items"]
    assert listing[0]["childRunId"] == child_run["id"]
    assert listing[0]["status"] == "running"

    events = (
        await client.get(
            "/api/v1/events",
            params={"entityType": "run", "entityId": run["id"]},
            headers=auth(agent_key),
        )
    ).json()["items"]
    started = [event for event in events if event["type"] == "run.child.started"]
    assert len(started) == 1
    assert started[0]["payload"]["childRunId"] == child_run["id"]


async def test_child_handles_are_scoped_to_their_tenant(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    await launch(client, agent_key, run["id"])

    _, foreign_key = make_tenant_directly(sync_engine, "other-tenant")
    response = await client.get(
        f"/api/v1/runs/{run['id']}/child-handles", headers=auth(foreign_key)
    )
    assert response.status_code == 404, response.text


# --- narrowing (S2) -----------------------------------------------------------


async def test_grant_beyond_the_parent_ceiling_is_rejected(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])

    response = await launch(
        client, agent_key, run["id"], grant={"permissions": ["tasks.read", "claims.manage"]}
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "child_grant_exceeds_parent"
    assert error["details"]["excess"]["permissions"] == ["claims.manage"]


async def test_child_cannot_write_beyond_its_ceiling(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    # The child key is deliberately STRONGER than its ceiling: it holds
    # artifacts.write, so a rejection can only come from the handle.
    _, child_key = await create_agent_with_key(
        client,
        admin_key,
        name="child-agent",
        permissions=["sessions.open", "tasks.read", "tasks.claim", "artifacts.write"],
    )
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    handle = (
        await launch(
            client,
            agent_key,
            run["id"],
            grant={"permissions": ["tasks.read", "tasks.claim", "sessions.open"]},
        )
    ).json()["childHandle"]
    _, child_run = await claim_and_run(client, child_key, handle["childTaskId"])

    checkpoint = await client.post(
        f"/api/v1/runs/{child_run['id']}/checkpoints",
        json={"kind": "progress", "data": {"step": 1}},
        headers=auth(child_key),
    )
    assert checkpoint.status_code == 201, checkpoint.text

    artifact = await client.post(
        "/api/v1/artifacts",
        json={"type": "document", "name": "report", "runId": child_run["id"]},
        headers=auth(child_key),
    )
    assert artifact.status_code == 403, artifact.text
    assert artifact.json()["error"]["code"] == "child_grant_exceeded"


async def test_grandchild_cannot_recover_what_its_parent_gave_up(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, child_key = await create_agent_with_key(client, admin_key, name="child-agent")
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    handle = (
        await launch(
            client,
            agent_key,
            run["id"],
            grant={"permissions": ["tasks.read", "tasks.write", "tasks.claim", "sessions.open"]},
        )
    ).json()["childHandle"]
    _, child_run = await claim_and_run(client, child_key, handle["childTaskId"])

    # The child key holds artifacts.write, but its handle does not grant it.
    over = await launch(
        client,
        child_key,
        child_run["id"],
        correlation_id="grandchild",
        idempotency_key="grandchild-1",
        grant={"permissions": ["artifacts.write"]},
    )
    assert over.status_code == 422, over.text
    assert over.json()["error"]["details"]["excess"]["permissions"] == ["artifacts.write"]

    ok = await launch(
        client,
        child_key,
        child_run["id"],
        correlation_id="grandchild",
        idempotency_key="grandchild-2",
        grant={"permissions": ["tasks.read"]},
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["childHandle"]["depth"] == 2


async def test_child_run_cannot_start_without_tasks_claim_in_its_grant(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, child_key = await create_agent_with_key(client, admin_key, name="child-agent")
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    handle = (
        await launch(client, agent_key, run["id"], grant={"permissions": ["tasks.read"]})
    ).json()["childHandle"]

    session = await open_session(client, child_key)
    claim = (
        await client.post(
            f"/api/v1/tasks/{handle['childTaskId']}:claim",
            json={"sessionId": session["id"]},
            headers=auth(child_key),
        )
    ).json()
    start = await client.post(
        f"/api/v1/tasks/{handle['childTaskId']}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(child_key),
    )
    assert start.status_code == 403, start.text
    assert start.json()["error"]["code"] == "child_grant_exceeded"


async def test_operator_oversight_is_not_bounded_by_the_child_grant(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, child_key = await create_agent_with_key(client, admin_key, name="child-agent")
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    handle = (
        await launch(
            client,
            agent_key,
            run["id"],
            grant={"permissions": ["tasks.read", "tasks.claim", "sessions.open"]},
        )
    ).json()["childHandle"]
    _, child_run = await claim_and_run(client, child_key, handle["childTaskId"])

    # The admin acts on the child run from outside; the child's ceiling bounds
    # the child, not oversight of it.
    cancel = await client.post(
        f"/api/v1/runs/{child_run['id']}:cancel",
        json={"reason": "operator stop"},
        headers=auth(admin_key),
    )
    assert cancel.status_code == 200, cancel.text


async def test_child_sees_and_uses_only_the_skills_its_handle_granted(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    parent, agent_key = await create_agent_with_key(client, admin_key)
    child, child_key = await create_agent_with_key(client, admin_key, name="child-agent")
    granted_skill = await register_skill(client, admin_key, "review", protocol="mcp")
    withheld_skill = await register_skill(client, admin_key, "deploy", protocol="mcp")
    for principal in (parent, child):
        await assign_skill(client, admin_key, principal["id"], granted_skill["id"])
        await assign_skill(client, admin_key, principal["id"], withheld_skill["id"])

    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    handle = (
        await launch(
            client,
            agent_key,
            run["id"],
            grant={
                "permissions": ["tasks.read", "tasks.claim", "sessions.open"],
                "skills": [f"review@{granted_skill['version']}"],
            },
        )
    ).json()["childHandle"]
    _, child_run = await claim_and_run(client, child_key, handle["childTaskId"])

    view = (
        await client.get(
            "/api/v1/tools", params={"runId": child_run["id"]}, headers=auth(child_key)
        )
    ).json()
    assert [tool["name"] for tool in view["items"]] == ["review"]
    withheld = await client.get(
        f"/api/v1/tools/{withheld_skill['id']}",
        params={"runId": child_run["id"]},
        headers=auth(child_key),
    )
    assert withheld.status_code == 404, withheld.text

    allowed = await client.post(
        f"/api/v1/runs/{child_run['id']}/actions",
        json={"action": "review the diff", "skill": "review"},
        headers=auth(child_key),
    )
    assert allowed.status_code == 201, allowed.text

    denied = await client.post(
        f"/api/v1/runs/{child_run['id']}/actions",
        json={"action": "deploy it", "skill": "deploy"},
        headers=auth(child_key),
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "child_grant_exceeded"


async def test_root_run_is_not_narrowed_by_any_grant(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    principal, agent_key = await create_agent_with_key(client, admin_key)
    skill = await register_skill(client, admin_key, "review", protocol="mcp")
    await assign_skill(client, admin_key, principal["id"], skill["id"])
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])

    view = (
        await client.get("/api/v1/tools", params={"runId": run["id"]}, headers=auth(agent_key))
    ).json()
    assert [tool["name"] for tool in view["items"]] == ["review"]

    action = await client.post(
        f"/api/v1/runs/{run['id']}/actions",
        json={"action": "review", "skill": "review"},
        headers=auth(agent_key),
    )
    assert action.status_code == 201, action.text


# --- resolution, reconnect and revocation (S3) --------------------------------


async def test_resolve_by_id_and_by_token(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    body = (await launch(client, agent_key, run["id"])).json()
    handle_id = body["childHandle"]["id"]
    token = body["handleToken"]

    by_id = await client.get(f"/api/v1/child-handles/{handle_id}", headers=auth(agent_key))
    by_token = await client.get(f"/api/v1/child-handles/{token}", headers=auth(agent_key))
    assert by_id.status_code == 200, by_id.text
    assert by_token.status_code == 200, by_token.text
    assert by_id.json()["childHandle"] == by_token.json()["childHandle"]

    # A valid id with a tampered secret is indistinguishable from a missing one.
    tampered = f"{token[:-4]}xxxx"
    forged = await client.get(f"/api/v1/child-handles/{tampered}", headers=auth(agent_key))
    assert forged.status_code == 404, forged.text

    garbage = await client.get("/api/v1/child-handles/not-a-handle", headers=auth(agent_key))
    assert garbage.status_code == 422
    assert garbage.json()["error"]["code"] == "invalid_child_handle_ref"


async def test_token_alone_grants_nothing_without_a_key(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    token = (await launch(client, agent_key, run["id"])).json()["handleToken"]

    anonymous = await client.get(f"/api/v1/child-handles/{token}")
    assert anonymous.status_code == 401, anonymous.text


async def test_foreign_tenant_cannot_resolve_a_handle_by_token(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    token = (await launch(client, agent_key, run["id"])).json()["handleToken"]

    _, foreign_key = make_tenant_directly(sync_engine, "other-tenant")
    response = await client.get(f"/api/v1/child-handles/{token}", headers=auth(foreign_key))
    assert response.status_code == 404, response.text


async def test_run_context_carries_child_handles_for_reconnect(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, child_key = await create_agent_with_key(client, admin_key, name="child-agent")
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    handle = (await launch(client, agent_key, run["id"])).json()["childHandle"]
    _, child_run = await claim_and_run(client, child_key, handle["childTaskId"])

    # Reconnect: no token, no transcript — just the authoritative context.
    context = (
        await client.get(f"/api/v1/runs/{run['id']}/context", headers=auth(agent_key))
    ).json()
    assert context["childHandles"]["hasMore"] is False
    restored = context["childHandles"]["items"][0]
    assert restored["id"] == handle["id"]
    assert restored["childRunId"] == child_run["id"]
    assert restored["status"] == "running"
    assert restored["correlationId"] == "review:migration"


async def test_revoke_is_idempotent_and_blocks_further_launches(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, child_key = await create_agent_with_key(client, admin_key, name="child-agent")
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    handle = (await launch(client, agent_key, run["id"])).json()["childHandle"]
    _, child_run = await claim_and_run(client, child_key, handle["childTaskId"])

    first = await client.post(
        f"/api/v1/child-handles/{handle['id']}:revoke",
        json={"reason": "no longer needed"},
        headers={**auth(agent_key), "Idempotency-Key": "revoke-1"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["childHandle"]["revokedAt"] is not None
    assert first.json()["childHandle"]["revokeReason"] == "no longer needed"
    # A revoked handle does not stop a child that is already running: the
    # child Run stays authoritative until someone cancels it.
    assert first.json()["childHandle"]["status"] == "running"

    second = await client.post(
        f"/api/v1/child-handles/{handle['id']}:revoke",
        json={"reason": "again"},
        headers={**auth(agent_key), "Idempotency-Key": "revoke-2"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["childHandle"]["revokedAt"] == first.json()["childHandle"]["revokedAt"]
    assert second.json()["childHandle"]["revokeReason"] == "no longer needed"

    # The withdrawn handle can no longer be used as a launch pad.
    grandchild = await launch(
        client,
        child_key,
        child_run["id"],
        correlation_id="grandchild",
        idempotency_key="grandchild-1",
    )
    assert grandchild.status_code == 409, grandchild.text
    assert grandchild.json()["error"]["code"] == "child_handle_revoked"


async def test_revoke_can_ask_the_child_to_stop_cooperatively(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, child_key = await create_agent_with_key(client, admin_key, name="child-agent")
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    handle = (await launch(client, agent_key, run["id"])).json()["childHandle"]
    _, child_run = await claim_and_run(client, child_key, handle["childTaskId"])

    response = await client.post(
        f"/api/v1/child-handles/{handle['id']}:revoke",
        json={"reason": "stop please", "cancelChild": True},
        headers={**auth(agent_key), "Idempotency-Key": "revoke-cancel"},
    )
    assert response.status_code == 200, response.text

    messages = (
        await client.get(
            f"/api/v1/runs/{child_run['id']}/control-messages", headers=auth(child_key)
        )
    ).json()["items"]
    assert [m["operation"] for m in messages] == ["request_cancel"]
    assert messages[0]["status"] == "accepted"

    # Repeating the revoke does not queue a second stop.
    again = await client.post(
        f"/api/v1/child-handles/{handle['id']}:revoke",
        json={"reason": "stop please", "cancelChild": True},
        headers={**auth(agent_key), "Idempotency-Key": "revoke-cancel-2"},
    )
    assert again.status_code == 200, again.text
    messages = (
        await client.get(
            f"/api/v1/runs/{child_run['id']}/control-messages", headers=auth(child_key)
        )
    ).json()["items"]
    assert len(messages) == 1


async def test_revoke_requires_the_parent_holder_or_claims_manage(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, other_key = await create_agent_with_key(client, admin_key, name="other-agent")
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    handle = (await launch(client, agent_key, run["id"])).json()["childHandle"]

    denied = await client.post(
        f"/api/v1/child-handles/{handle['id']}:revoke",
        json={"reason": "not mine"},
        headers={**auth(other_key), "Idempotency-Key": "revoke-foreign"},
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "run_holder_mismatch"

    # claims.manage is oversight and does not need to hold the parent run.
    allowed = await client.post(
        f"/api/v1/child-handles/{handle['id']}:revoke",
        json={"reason": "operator"},
        headers={**auth(admin_key), "Idempotency-Key": "revoke-admin"},
    )
    assert allowed.status_code == 200, allowed.text


async def test_expired_handle_stops_being_a_launch_pad(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, child_key = await create_agent_with_key(client, admin_key, name="child-agent")
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    handle = (await launch(client, agent_key, run["id"])).json()["childHandle"]
    _, child_run = await claim_and_run(client, child_key, handle["childTaskId"])

    _expire_handle(sync_engine, handle["id"])

    resolved = await client.get(f"/api/v1/child-handles/{handle['id']}", headers=auth(agent_key))
    assert resolved.status_code == 200, resolved.text
    # The child is still running and stays visible: expiry is a fact about the
    # locator, not about the execution.
    assert resolved.json()["childHandle"]["status"] == "running"

    grandchild = await launch(
        client,
        child_key,
        child_run["id"],
        correlation_id="grandchild",
        idempotency_key="grandchild-expired",
    )
    assert grandchild.status_code == 409, grandchild.text
    assert grandchild.json()["error"]["code"] == "child_handle_expired"


# --- bounded immutable result (S4) --------------------------------------------


async def test_child_success_records_a_hashed_bounded_result(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    # The parent must itself hold artifacts.write: it cannot grant what it
    # does not have, which is the whole point of the ceiling.
    _, agent_key = await create_agent_with_key(
        client,
        admin_key,
        permissions=[
            "sessions.open",
            "tasks.read",
            "tasks.write",
            "tasks.claim",
            "artifacts.write",
            "events.read",
        ],
    )
    _, child_key = await create_agent_with_key(
        client,
        admin_key,
        name="child-agent",
        permissions=["sessions.open", "tasks.read", "tasks.claim", "artifacts.write"],
    )
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    handle = (
        await launch(
            client,
            agent_key,
            run["id"],
            grant={
                "permissions": ["tasks.read", "tasks.claim", "sessions.open", "artifacts.write"]
            },
        )
    ).json()["childHandle"]
    _, child_run = await claim_and_run(client, child_key, handle["childTaskId"])

    artifact = (
        await client.post(
            "/api/v1/artifacts",
            json={"type": "document", "name": "report", "runId": child_run["id"]},
            headers=auth(child_key),
        )
    ).json()
    succeed = await client.post(
        f"/api/v1/runs/{child_run['id']}:succeed",
        json={"output": {"summary": "roundtrip verified", "data": {"checked": 3}}},
        headers=auth(child_key),
    )
    assert succeed.status_code == 200, succeed.text

    resolved = (
        await client.get(f"/api/v1/child-handles/{handle['id']}", headers=auth(agent_key))
    ).json()["childHandle"]
    assert resolved["status"] == "succeeded"
    result = resolved["result"]
    assert result["outcome"] == "succeeded"
    assert result["summary"] == "roundtrip verified"
    assert result["data"] == {"checked": 3}
    # Evidence is referenced, not copied.
    assert result["artifactRefs"] == [artifact["id"]]
    assert result["resultHash"].startswith("sha256:")

    events = (
        await client.get(
            "/api/v1/events",
            params={"entityType": "run", "entityId": run["id"]},
            headers=auth(agent_key),
        )
    ).json()["items"]
    resolved_events = [event for event in events if event["type"] == "run.child.resolved"]
    assert len(resolved_events) == 1
    payload = resolved_events[0]["payload"]
    assert payload["resultHash"] == result["resultHash"]
    assert payload["outcome"] == "succeeded"
    assert "summary" not in payload


async def test_failed_child_still_produces_a_result(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, child_key = await create_agent_with_key(client, admin_key, name="child-agent")
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    handle = (await launch(client, agent_key, run["id"])).json()["childHandle"]
    _, child_run = await claim_and_run(client, child_key, handle["childTaskId"])

    fail = await client.post(
        f"/api/v1/runs/{child_run['id']}:fail",
        json={"failureReason": "migration check failed"},
        headers=auth(child_key),
    )
    assert fail.status_code == 200, fail.text

    resolved = (
        await client.get(f"/api/v1/child-handles/{handle['id']}", headers=auth(agent_key))
    ).json()["childHandle"]
    assert resolved["status"] == "failed"
    assert resolved["result"]["outcome"] == "failed"
    assert resolved["result"]["summary"] == "migration check failed"
    assert resolved["result"]["data"] == {}


async def test_oversized_child_output_is_rejected_and_the_run_keeps_running(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, child_key = await create_agent_with_key(client, admin_key, name="child-agent")
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    handle = (await launch(client, agent_key, run["id"])).json()["childHandle"]
    _, child_run = await claim_and_run(client, child_key, handle["childTaskId"])

    too_big = await client.post(
        f"/api/v1/runs/{child_run['id']}:succeed",
        json={"output": {"summary": "done", "data": {"blob": "x" * (32 * 1024)}}},
        headers=auth(child_key),
    )
    assert too_big.status_code == 422, too_big.text
    assert too_big.json()["error"]["code"] == "payload_too_large"

    state = (await client.get(f"/api/v1/runs/{child_run['id']}", headers=auth(child_key))).json()
    assert state["status"] == "running"

    # Shrinking the payload lets the same run finish.
    retry = await client.post(
        f"/api/v1/runs/{child_run['id']}:succeed",
        json={"output": {"summary": "done", "data": {"blob": "see the artifact"}}},
        headers=auth(child_key),
    )
    assert retry.status_code == 200, retry.text


async def test_a_recorded_result_cannot_be_rewritten(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, child_key = await create_agent_with_key(client, admin_key, name="child-agent")
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    handle = (await launch(client, agent_key, run["id"])).json()["childHandle"]
    child_claim, child_run = await claim_and_run(client, child_key, handle["childTaskId"])
    await client.post(
        f"/api/v1/runs/{child_run['id']}:fail",
        json={"failureReason": "first attempt failed"},
        headers=auth(child_key),
    )
    # :fail finalizes the run but keeps the lease, so a retry releases first.
    await client.post(f"/api/v1/claims/{child_claim['id']}:release", headers=auth(child_key))

    with pytest.raises(Exception, match="immutable"), sync_engine.begin() as conn:
        conn.execute(text("UPDATE run_child_results SET summary = 'rewritten'"))

    # A retry of the child task does not overwrite the result the parent read.
    _, second_run = await claim_and_run(client, child_key, handle["childTaskId"])
    await client.post(
        f"/api/v1/runs/{second_run['id']}:succeed",
        json={"output": {"summary": "second attempt succeeded"}},
        headers=auth(child_key),
    )
    resolved = (
        await client.get(f"/api/v1/child-handles/{handle['id']}", headers=auth(agent_key))
    ).json()["childHandle"]
    assert resolved["result"]["summary"] == "first attempt failed"
    assert resolved["childRunId"] == child_run["id"]


async def test_a_run_without_a_handle_finishes_exactly_as_before(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])

    succeed = await client.post(
        f"/api/v1/runs/{run['id']}:succeed",
        json={"output": {"anything": "x" * 100}},
        headers=auth(agent_key),
    )
    assert succeed.status_code == 200, succeed.text
    assert succeed.json()["run"]["output"] == {"anything": "x" * 100}


# --- cancellation policy (S5) -------------------------------------------------


async def _child_under_policy(
    client: httpx.AsyncClient,
    admin_key: str,
    agent_key: str,
    child_key: str,
    run: dict[str, Any],
    *,
    policy: str,
    correlation_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    handle = (
        await launch(
            client,
            agent_key,
            run["id"],
            correlation_id=correlation_id,
            idempotency_key=f"launch-{correlation_id}",
            cancellationPolicy=policy,
        )
    ).json()["childHandle"]
    _, child_run = await claim_and_run(client, child_key, handle["childTaskId"])
    return handle, child_run


async def _apply_request_cancel(
    client: httpx.AsyncClient, admin_key: str, agent_key: str, run_id: str, claim: dict[str, Any]
) -> None:
    run_state = (await client.get(f"/api/v1/runs/{run_id}", headers=auth(agent_key))).json()
    message = (
        await client.post(
            f"/api/v1/runs/{run_id}/control-messages",
            json={
                "operation": "request_cancel",
                "causalPosition": "turn:1",
                "reason": "human stop",
                "expectedRunVersion": run_state["version"],
            },
            headers={**auth(admin_key), "Idempotency-Key": f"stop-{run_id}"},
        )
    ).json()
    run_state = (await client.get(f"/api/v1/runs/{run_id}", headers=auth(agent_key))).json()
    ack = await client.post(
        f"/api/v1/runs/{run_id}/control-messages/{message['controlMessage']['id']}:acknowledge",
        json={
            "status": "applied",
            "claimId": claim["id"],
            "fencingToken": claim["fencingToken"],
            "expectedRunVersion": run_state["version"],
            "expectedMessageVersion": message["controlMessage"]["version"],
            "safeBoundary": "turn:1:complete",
        },
        headers=auth(agent_key),
    )
    assert ack.status_code == 200, ack.text


async def test_cooperative_cancel_follows_policy(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, child_key = await create_agent_with_key(client, admin_key, name="child-agent")
    task = await create_task(client, admin_key)
    claim, run = await claim_and_run(client, agent_key, task["id"])

    _, cascading_run = await _child_under_policy(
        client,
        admin_key,
        agent_key,
        child_key,
        run,
        policy="cascade_cooperative",
        correlation_id="cascading",
    )
    _, detached_run = await _child_under_policy(
        client,
        admin_key,
        agent_key,
        child_key,
        run,
        policy="detach",
        correlation_id="detached",
    )

    await _apply_request_cancel(client, admin_key, agent_key, run["id"], claim)

    cascading_messages = (
        await client.get(
            f"/api/v1/runs/{cascading_run['id']}/control-messages", headers=auth(child_key)
        )
    ).json()["items"]
    assert [m["operation"] for m in cascading_messages] == ["request_cancel"]
    assert cascading_messages[0]["reason"] == "human stop"

    detached_messages = (
        await client.get(
            f"/api/v1/runs/{detached_run['id']}/control-messages", headers=auth(child_key)
        )
    ).json()["items"]
    assert detached_messages == []


async def test_force_cancel_cascades_regardless_of_policy(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, child_key = await create_agent_with_key(client, admin_key, name="child-agent")
    task = await create_task(client, admin_key)
    _, run = await claim_and_run(client, agent_key, task["id"])
    handle, detached_run = await _child_under_policy(
        client,
        admin_key,
        agent_key,
        child_key,
        run,
        policy="detach",
        correlation_id="detached",
    )

    run_state = (await client.get(f"/api/v1/runs/{run['id']}", headers=auth(agent_key))).json()
    forced = await client.post(
        f"/api/v1/runs/{run['id']}/control-messages",
        json={
            "operation": "force_cancel",
            "causalPosition": "turn:1",
            "reason": "governance stop",
            "expectedRunVersion": run_state["version"],
        },
        headers={**auth(admin_key), "Idempotency-Key": "force-stop"},
    )
    assert forced.status_code == 201, forced.text

    child_state = (
        await client.get(f"/api/v1/runs/{detached_run['id']}", headers=auth(admin_key))
    ).json()
    assert child_state["status"] == "cancelled"

    resolved = (
        await client.get(f"/api/v1/child-handles/{handle['id']}", headers=auth(admin_key))
    ).json()["childHandle"]
    assert resolved["status"] == "cancelled"
    assert resolved["result"]["outcome"] == "cancelled"
