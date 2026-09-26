"""Durable Active Turn Control HTTP contract (TASK-000003)."""

from typing import Any

import httpx

from tests.helpers import (
    auth,
    backdate_expiry,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    make_tenant_directly,
    open_session,
)


async def _claim_and_run(
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
    run_response = await client.post(
        f"/api/v1/tasks/{task_id}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(agent_key),
    )
    assert run_response.status_code == 201, run_response.text
    return claim, run_response.json()


async def test_create_steer_control_message(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await _claim_and_run(client, agent_key, task["id"])

    response = await client.post(
        f"/api/v1/runs/{run['id']}/control-messages",
        json={
            "operation": "steer",
            "causalPosition": "turn:1/tool-batch:0",
            "directive": "Run the migration roundtrip before continuing",
            "reason": "human correction",
            "expectedRunVersion": run["version"],
        },
        headers={**auth(admin_key), "Idempotency-Key": "steer-turn-1"},
    )

    assert response.status_code == 201, response.text
    assert response.headers["location"].endswith(
        f"/runs/{run['id']}/control-messages/{response.json()['controlMessage']['id']}"
    )
    body = response.json()
    assert body["runVersion"] == run["version"] + 1
    message = body["controlMessage"]
    assert message["taskId"] == task["id"]
    assert message["runId"] == run["id"]
    assert message["seq"] == 1
    assert message["operation"] == "steer"
    assert message["status"] == "accepted"
    assert message["causalPosition"] == "turn:1/tool-batch:0"
    assert message["directive"] == "Run the migration roundtrip before continuing"
    assert message["reason"] == "human correction"
    assert message["safeBoundary"] is None
    assert message["idempotencyKey"] == "steer-turn-1"
    assert message["acknowledgedByPrincipalId"] is None
    assert message["version"] == 1
    assert message["resolvedAt"] is None

    events = (
        await client.get(
            "/api/v1/events",
            params={"entityType": "run", "entityId": run["id"]},
            headers=auth(agent_key),
        )
    ).json()["items"]
    accepted = [event for event in events if event["type"] == "run.control_message.accepted"]
    assert len(accepted) == 1
    assert accepted[0]["payload"] == {
        "taskId": task["id"],
        "controlMessageId": message["id"],
        "seq": 1,
        "operation": "steer",
        "status": "accepted",
        "causalPosition": "turn:1/tool-batch:0",
    }
    assert "directive" not in accepted[0]["payload"]
    assert "reason" not in accepted[0]["payload"]


async def test_holder_acknowledges_control_message(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    claim, run = await _claim_and_run(client, agent_key, task["id"])
    created_response = await client.post(
        f"/api/v1/runs/{run['id']}/control-messages",
        json={
            "operation": "steer",
            "causalPosition": "turn:1/model:waiting",
            "directive": "Use the safer migration path",
            "expectedRunVersion": run["version"],
        },
        headers={**auth(admin_key), "Idempotency-Key": "steer-ack-1"},
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()
    message = created["controlMessage"]

    response = await client.post(
        f"/api/v1/runs/{run['id']}/control-messages/{message['id']}:acknowledge",
        json={
            "status": "applied",
            "claimId": claim["id"],
            "fencingToken": claim["fencingToken"],
            "expectedRunVersion": created["runVersion"],
            "expectedMessageVersion": message["version"],
            "safeBoundary": "model:1:cancelled-before-history",
        },
        headers={**auth(agent_key), "Idempotency-Key": "steer-ack-1-applied"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runVersion"] == created["runVersion"] + 1
    assert body["controlMessage"]["status"] == "applied"
    assert body["controlMessage"]["version"] == 2
    assert body["controlMessage"]["safeBoundary"] == "model:1:cancelled-before-history"
    assert body["controlMessage"]["acknowledgedByPrincipalId"] == agent["id"]
    assert body["controlMessage"]["resolvedAt"] is not None

    events = (
        await client.get(
            "/api/v1/events",
            params={"entityType": "run", "entityId": run["id"]},
            headers=auth(agent_key),
        )
    ).json()["items"]
    applied = [event for event in events if event["type"] == "run.control_message.applied"]
    assert len(applied) == 1
    assert applied[0]["payload"]["safeBoundary"] == "model:1:cancelled-before-history"
    assert "directive" not in applied[0]["payload"]


async def test_list_control_messages_recovers_from_opaque_cursor(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await _claim_and_run(client, agent_key, task["id"])

    run_version = run["version"]
    for seq, operation in enumerate(("queue", "steer", "redirect"), start=1):
        response = await client.post(
            f"/api/v1/runs/{run['id']}/control-messages",
            json={
                "operation": operation,
                "causalPosition": f"turn:{seq}",
                "directive": f"directive-{seq}",
                "expectedRunVersion": run_version,
            },
            headers={**auth(admin_key), "Idempotency-Key": f"recovery-{seq}"},
        )
        assert response.status_code == 201, response.text
        run_version = response.json()["runVersion"]

    first = await client.get(
        f"/api/v1/runs/{run['id']}/control-messages",
        params={"limit": 2},
        headers=auth(agent_key),
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert [item["seq"] for item in first_body["items"]] == [1, 2]
    assert first_body["nextCursor"].startswith("rc1_")
    assert first_body["hasMore"] is True

    second = await client.get(
        f"/api/v1/runs/{run['id']}/control-messages",
        params={"limit": 2, "cursor": first_body["nextCursor"]},
        headers=auth(agent_key),
    )
    assert second.status_code == 200, second.text
    assert [item["seq"] for item in second.json()["items"]] == [3]
    assert second.json()["nextCursor"].startswith("rc1_")
    assert second.json()["hasMore"] is False

    context = await client.get(f"/api/v1/runs/{run['id']}/context", headers=auth(agent_key))
    assert context.status_code == 200, context.text
    assert [item["seq"] for item in context.json()["pendingControlMessages"]] == [1, 2, 3]


async def test_request_cancel_blocks_actions_only_after_safe_boundary(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    claim, run = await _claim_and_run(client, agent_key, task["id"])

    requested_response = await client.post(
        f"/api/v1/runs/{run['id']}/control-messages",
        json={
            "operation": "request_cancel",
            "causalPosition": "turn:2/tool:running",
            "reason": "operator requested cooperative stop",
            "expectedRunVersion": run["version"],
        },
        headers={**auth(admin_key), "Idempotency-Key": "cancel-turn-2"},
    )
    assert requested_response.status_code == 201, requested_response.text
    requested = requested_response.json()
    assert requested["controlMessage"]["status"] == "accepted"

    # The in-flight action may finish before the harness reaches its boundary.
    before = await client.post(
        f"/api/v1/runs/{run['id']}/actions",
        json={"action": "tool.in_flight_completed"},
        headers=auth(agent_key),
    )
    assert before.status_code == 201, before.text

    # The action append changed Run-independent audit only, so Run version is
    # still the one returned with the control request.
    applied = await client.post(
        f"/api/v1/runs/{run['id']}/control-messages/"
        f"{requested['controlMessage']['id']}:acknowledge",
        json={
            "status": "applied",
            "claimId": claim["id"],
            "fencingToken": claim["fencingToken"],
            "expectedRunVersion": requested["runVersion"],
            "expectedMessageVersion": 1,
            "safeBoundary": "tool-batch:2:complete",
        },
        headers={**auth(agent_key), "Idempotency-Key": "cancel-turn-2-applied"},
    )
    assert applied.status_code == 200, applied.text

    after = await client.post(
        f"/api/v1/runs/{run['id']}/actions",
        json={"action": "tool.must_not_start"},
        headers=auth(agent_key),
    )
    assert after.status_code == 409, after.text
    assert after.json()["error"]["code"] == "run_cancel_requested"

    run_after = await client.get(f"/api/v1/runs/{run['id']}", headers=auth(agent_key))
    assert run_after.json()["cancelRequestedAt"] is not None
    events = (
        await client.get(
            "/api/v1/events",
            params={"entityType": "run", "entityId": run["id"]},
            headers=auth(agent_key),
        )
    ).json()["items"]
    assert len([event for event in events if event["type"] == "run.cancel_requested"]) == 1


async def test_force_cancel_releases_claim_and_fences_owner_writes(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    claim, run = await _claim_and_run(client, agent_key, task["id"])

    response = await client.post(
        f"/api/v1/runs/{run['id']}/control-messages",
        json={
            "operation": "force_cancel",
            "causalPosition": "operator:emergency-stop",
            "reason": "policy revoked execution authority",
            "expectedRunVersion": run["version"],
        },
        headers={**auth(admin_key), "Idempotency-Key": "force-cancel-1"},
    )
    assert response.status_code == 201, response.text
    message = response.json()["controlMessage"]
    assert message["status"] == "applied"
    assert message["safeBoundary"] == "server:force_cancel"

    run_after = await client.get(f"/api/v1/runs/{run['id']}", headers=auth(agent_key))
    assert run_after.status_code == 200
    assert run_after.json()["status"] == "cancelled"
    assert run_after.json()["failureReason"] == "policy revoked execution authority"

    claim_after = await client.get(f"/api/v1/claims/{claim['id']}", headers=auth(agent_key))
    assert claim_after.json()["status"] == "released"
    assert claim_after.json()["releaseReason"] == "force_cancel"
    task_after = await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(agent_key))
    assert task_after.json()["status"] == "todo"
    assert task_after.json()["activeClaimId"] is None

    zombie_action = await client.post(
        f"/api/v1/runs/{run['id']}/actions",
        json={"action": "zombie.write"},
        headers=auth(agent_key),
    )
    assert zombie_action.status_code == 409
    assert zombie_action.json()["error"]["code"] == "run_not_active"


async def test_force_cancel_cascades_to_spawned_child_run(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    parent_task = await create_task(client, admin_key, title="parent")
    child_task = await create_task(client, admin_key, title="child")
    relation = await client.post(
        f"/api/v1/tasks/{child_task['id']}/relations",
        json={"toTask": parent_task["id"], "type": "spawned_by"},
        headers=auth(admin_key),
    )
    assert relation.status_code == 201, relation.text
    _, parent_run = await _claim_and_run(client, agent_key, parent_task["id"])
    child_claim, child_run = await _claim_and_run(client, agent_key, child_task["id"])

    response = await client.post(
        f"/api/v1/runs/{parent_run['id']}/control-messages",
        json={
            "operation": "force_cancel",
            "causalPosition": "operator:parent-stop",
            "reason": "cancel the execution tree",
            "expectedRunVersion": parent_run["version"],
        },
        headers={**auth(admin_key), "Idempotency-Key": "force-parent-tree"},
    )
    assert response.status_code == 201, response.text

    child_after = await client.get(f"/api/v1/runs/{child_run['id']}", headers=auth(agent_key))
    assert child_after.json()["status"] == "cancelled"
    child_claim_after = await client.get(
        f"/api/v1/claims/{child_claim['id']}", headers=auth(agent_key)
    )
    assert child_claim_after.json()["status"] == "released"
    messages = await client.get(
        f"/api/v1/runs/{child_run['id']}/control-messages", headers=auth(agent_key)
    )
    assert [(item["operation"], item["status"]) for item in messages.json()["items"]] == [
        ("force_cancel", "applied")
    ]


async def test_legacy_request_cancel_materializes_typed_message(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await _claim_and_run(client, agent_key, task["id"])

    response = await client.post(
        f"/api/v1/runs/{run['id']}:request-cancel",
        json={"reason": "legacy client requested stop"},
        headers=auth(admin_key),
    )
    assert response.status_code == 200, response.text
    listed = await client.get(f"/api/v1/runs/{run['id']}/control-messages", headers=auth(agent_key))
    assert [(item["operation"], item["status"]) for item in listed.json()["items"]] == [
        ("request_cancel", "accepted")
    ]


async def test_control_message_idempotency_and_optimistic_conflict(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await _claim_and_run(client, agent_key, task["id"])
    body = {
        "operation": "queue",
        "causalPosition": "turn:1",
        "directive": "Continue with the next intent",
        "expectedRunVersion": run["version"],
    }
    headers = {**auth(admin_key), "Idempotency-Key": "same-control-key"}

    first = await client.post(
        f"/api/v1/runs/{run['id']}/control-messages", json=body, headers=headers
    )
    assert first.status_code == 201, first.text
    replay = await client.post(
        f"/api/v1/runs/{run['id']}/control-messages", json=body, headers=headers
    )
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()

    reused = await client.post(
        f"/api/v1/runs/{run['id']}/control-messages",
        json={**body, "directive": "different"},
        headers=headers,
    )
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "idempotency_key_reused"

    stale = await client.post(
        f"/api/v1/runs/{run['id']}/control-messages",
        json={**body, "causalPosition": "turn:2"},
        headers={**auth(admin_key), "Idempotency-Key": "stale-version"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "run_version_conflict"


async def test_control_message_authorization_tenant_and_stale_claim(
    client: httpx.AsyncClient, sync_engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    claim, run = await _claim_and_run(client, agent_key, task["id"])

    forbidden = await client.post(
        f"/api/v1/runs/{run['id']}/control-messages",
        json={
            "operation": "force_cancel",
            "causalPosition": "agent:self",
            "reason": "not authorized",
            "expectedRunVersion": run["version"],
        },
        headers={**auth(agent_key), "Idempotency-Key": "unauthorized-force"},
    )
    assert forbidden.status_code == 403

    created = await client.post(
        f"/api/v1/runs/{run['id']}/control-messages",
        json={
            "operation": "steer",
            "causalPosition": "turn:1",
            "directive": "Stop after the current safe boundary",
            "expectedRunVersion": run["version"],
        },
        headers={**auth(admin_key), "Idempotency-Key": "stale-ack"},
    )
    assert created.status_code == 201, created.text
    created_body = created.json()
    backdate_expiry(sync_engine, "task_claims", claim["id"])
    stale_ack = await client.post(
        f"/api/v1/runs/{run['id']}/control-messages/"
        f"{created_body['controlMessage']['id']}:acknowledge",
        json={
            "status": "applied",
            "claimId": claim["id"],
            "fencingToken": claim["fencingToken"],
            "expectedRunVersion": created_body["runVersion"],
            "expectedMessageVersion": 1,
            "safeBoundary": "turn:1:complete",
        },
        headers={**auth(agent_key), "Idempotency-Key": "stale-ack-applied"},
    )
    assert stale_ack.status_code == 409
    assert stale_ack.json()["error"]["code"] == "stale_claim"

    _, other_key = make_tenant_directly(sync_engine, "other-control-tenant")
    hidden = await client.get(f"/api/v1/runs/{run['id']}/control-messages", headers=auth(other_key))
    assert hidden.status_code == 404


async def test_control_message_rejects_credential_like_payload(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await _claim_and_run(client, agent_key, task["id"])
    response = await client.post(
        f"/api/v1/runs/{run['id']}/control-messages",
        json={
            "operation": "steer",
            "causalPosition": "turn:secret",
            "directive": "Bearer abcdefghijklmnopqrstuvwxyz",
            "expectedRunVersion": run["version"],
        },
        headers={**auth(admin_key), "Idempotency-Key": "unsafe-control"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsafe_control_payload"
