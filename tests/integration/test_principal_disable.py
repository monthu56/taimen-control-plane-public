"""``POST /principals/{id}:disable`` (CP-ADR-0077).

One call takes a person or an agent out: entry closes for every credential it
has, its work goes back to the queue under the ordinary release rules, and
the journal says what happened. A second call changes nothing.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from platform_auth.testing import SigningKey
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.infrastructure.auth.iam import SCOPE_READ, SCOPE_WRITE
from tests.helpers import (
    auth,
    backdate_expiry,
    claim_task,
    create_agent_with_key,
    create_task,
    do_bootstrap,
)
from tests.helpers import open_session as open_work_session
from tests.integration.test_agent_registry import _link, _publish, _tenant, coder_spec
from tests.integration.test_child_run_handle import claim_and_run, launch
from tests.integration.test_iam_bindings import create_principal, upsert
from tests.integration.test_iam_enforcement import ISSUER, enable_iam
from tests.integration.test_skill_invocations_m21 import (
    CALLER_PERMISSIONS,
    EXECUTOR_PERMISSIONS,
    claim,
    invoke,
    published,
)


async def disable(
    client: httpx.AsyncClient, key: str, principal_id: str, **body: Any
) -> httpx.Response:
    return await client.post(
        f"/api/v1/principals/{principal_id}:disable",
        json=body or None,
        headers=auth(key),
    )


async def events_of(
    client: httpx.AsyncClient, key: str, entity_type: str, entity_id: str
) -> list[dict[str, Any]]:
    response = await client.get(
        "/api/v1/events",
        params={"entityType": entity_type, "entityId": entity_id},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def test_disabled_agent_loses_entry_its_claims_and_its_sessions(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(client, admin_key)
    work_session = await open_work_session(client, agent_key)
    task = await create_task(client, admin_key)
    claimed = await claim_task(client, agent_key, task["id"], work_session["id"])
    assert claimed.status_code == 200, claimed.text
    claim = claimed.json()
    task_before = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert task_before["status"] == "in_progress"

    response = await disable(client, admin_key, agent["id"], reason="left the team")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "disabled"

    # The key is still valid as a key, but its principal no longer acts.
    denied = await client.get("/api/v1/tasks", headers=auth(agent_key))
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "principal_not_active"

    # Its work went back to the queue under the type's release rules.
    claim_now = (await client.get(f"/api/v1/claims/{claim['id']}", headers=auth(admin_key))).json()
    assert claim_now["status"] == "released"
    assert claim_now["releaseReason"] == "principal_disabled"
    task_now = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert task_now["status"] == "todo"
    assert task_now["activeClaimId"] is None

    session_now = (
        await client.get(f"/api/v1/sessions/{work_session['id']}", headers=auth(admin_key))
    ).json()
    assert session_now["status"] == "closed"
    assert session_now["endedAt"] is not None

    disabled_event = (await events_of(client, admin_key, "principal", agent["id"]))[-1]
    assert disabled_event["type"] == "principal.disabled"
    assert disabled_event["payload"] == {
        "kind": "agent",
        "previousStatus": "active",
        "reason": "left the team",
        "revokedBindings": 0,
        "revokedDelegations": 0,
        "closedSessions": 1,
        "releasedClaims": 1,
        "failedRuns": 0,
        "withdrawnInvocations": 0,
    }
    released = await events_of(client, admin_key, "claim", claim["id"])
    assert released[-1]["type"] == "claim.released"
    assert released[-1]["payload"]["reason"] == "principal_disabled"
    closed = await events_of(client, admin_key, "session", work_session["id"])
    assert closed[-1]["type"] == "session.closed"
    assert closed[-1]["payload"] == {
        "releasedClaims": [claim["id"]],
        "reason": "principal_disabled",
    }


async def test_repeated_disable_is_200_and_changes_nothing(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    human = await create_principal(client, admin_key, kind="human")

    first = await disable(client, admin_key, human["id"])
    assert first.status_code == 200, first.text
    events_after_first = await events_of(client, admin_key, "principal", human["id"])

    again = await disable(client, admin_key, human["id"], reason="once more")
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert await events_of(client, admin_key, "principal", human["id"]) == events_after_first
    assert [e["type"] for e in events_after_first] == ["principal.created", "principal.disabled"]
    assert events_after_first[-1]["payload"]["reason"] is None


async def test_disabled_human_iam_token_is_refused_despite_a_warm_cache(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """The positive answer cached for the identity must not outlive the disable."""
    signing_key = SigningKey.generate()
    enable_iam(app, signing_key, ttl_seconds=600.0)
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    human = await create_principal(client, admin_key, kind="human")
    iam_tenant, iam_principal = uuid.uuid4(), uuid.uuid4()
    binding = await upsert(
        client,
        admin_key,
        human["id"],
        permissions=["tasks.read", "tasks.write"],
        issuer=ISSUER,
        iamTenantId=str(iam_tenant),
        iamPrincipalId=str(iam_principal),
    )
    assert binding.status_code == 201, binding.text
    token = signing_key.issue(
        subject=iam_principal,
        tenant_id=iam_tenant,
        scopes=[SCOPE_READ, SCOPE_WRITE],
        ttl_seconds=3600,
    )
    assert (await client.get("/api/v1/tasks", headers=auth(token))).status_code == 200

    response = await disable(client, admin_key, human["id"])
    assert response.status_code == 200, response.text

    assert (await client.get("/api/v1/tasks", headers=auth(token))).status_code == 401
    listed = await client.get(
        f"/api/v1/principals/{human['id']}/iam-bindings", headers=auth(admin_key)
    )
    assert [b["status"] for b in listed.json()["items"]] == ["revoked"]
    revoked = await events_of(client, admin_key, "iam_binding", binding.json()["id"])
    assert revoked[-1]["type"] == "iam_binding.revoked"
    event = (await events_of(client, admin_key, "principal", human["id"]))[-1]
    assert event["payload"]["revokedBindings"] == 1

    # A binding cannot be reopened on a disabled principal either.
    reopened = await upsert(
        client,
        admin_key,
        human["id"],
        permissions=["tasks.read"],
        issuer=ISSUER,
        iamTenantId=str(iam_tenant),
        iamPrincipalId=str(iam_principal),
    )
    assert reopened.status_code == 422
    assert reopened.json()["error"]["code"] == "principal_not_active"


async def test_refusals(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    service = await create_principal(client, admin_key, kind="service")
    human = await create_principal(client, admin_key, kind="human")
    _, reader_key = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["principals.read"]
    )

    as_service = await disable(client, admin_key, service["id"])
    assert as_service.status_code == 422
    assert as_service.json()["error"]["code"] == "principal_kind_not_disableable"

    itself = await disable(client, admin_key, body["adminPrincipal"]["id"])
    assert itself.status_code == 409
    assert itself.json()["error"]["code"] == "cannot_disable_self"

    no_right = await disable(client, reader_key, human["id"])
    assert no_right.status_code == 403

    missing = await disable(client, admin_key, str(uuid.uuid4()))
    assert missing.status_code == 404

    unchanged = await client.get(f"/api/v1/principals/{human['id']}", headers=auth(admin_key))
    assert unchanged.json()["status"] == "active"


async def get(client: httpx.AsyncClient, key: str, path: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/{path}", headers=auth(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_a_running_run_on_a_freed_claim_fails(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(client, admin_key)
    work_session = await open_work_session(client, agent_key)
    task = await create_task(client, admin_key)
    claim = (await claim_task(client, agent_key, task["id"], work_session["id"])).json()
    started = await client.post(
        f"/api/v1/tasks/{task['id']}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(agent_key),
    )
    assert started.status_code == 201, started.text
    run = started.json()

    response = await disable(client, admin_key, agent["id"])
    assert response.status_code == 200, response.text

    run_now = await get(client, admin_key, f"runs/{run['id']}")
    assert (run_now["status"], run_now["failureReason"]) == ("failed", "principal_disabled")
    assert run_now["finishedAt"] is not None
    failed = (await events_of(client, admin_key, "run", run["id"]))[-1]
    assert failed["type"] == "run.failed"
    assert failed["payload"]["reason"] == "principal_disabled"
    event = (await events_of(client, admin_key, "principal", agent["id"]))[-1]
    assert (event["payload"]["releasedClaims"], event["payload"]["failedRuns"]) == (1, 1)

    # The task is free: another agent claims it and starts a fresh run.
    _, other_key = await create_agent_with_key(client, admin_key, name="agent-2")
    other_session = await open_work_session(client, other_key)
    again = await claim_task(client, other_key, task["id"], other_session["id"])
    assert again.status_code == 200, again.text


async def test_a_stale_session_is_closed_and_its_claim_freed(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(client, admin_key)
    work_session = await open_work_session(client, agent_key)
    task = await create_task(client, admin_key)
    claim = (await claim_task(client, agent_key, task["id"], work_session["id"])).json()
    backdate_expiry(sync_engine, "sessions", work_session["id"])
    heartbeat = await client.post(
        f"/api/v1/sessions/{work_session['id']}:heartbeat", headers=auth(agent_key)
    )
    assert heartbeat.status_code == 409
    assert (await get(client, admin_key, f"sessions/{work_session['id']}"))["status"] == "stale"
    assert (await get(client, admin_key, f"claims/{claim['id']}"))["status"] == "active"

    response = await disable(client, admin_key, agent["id"])
    assert response.status_code == 200, response.text

    assert (await get(client, admin_key, f"sessions/{work_session['id']}"))["status"] == "closed"
    claim_now = await get(client, admin_key, f"claims/{claim['id']}")
    assert (claim_now["status"], claim_now["releaseReason"]) == ("released", "principal_disabled")
    assert (await get(client, admin_key, f"tasks/{task['id']}"))["status"] == "todo"


async def test_a_claim_held_through_another_principals_session_is_freed(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """No route hands out such a claim today, but the claim row allows it."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    human = await create_principal(client, admin_key, kind="human")
    _, agent_key = await create_agent_with_key(client, admin_key)
    work_session = await open_work_session(client, agent_key)
    task = await create_task(client, admin_key)
    claim = (await claim_task(client, agent_key, task["id"], work_session["id"])).json()
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE task_claims SET holder_id = :holder WHERE id = :claim_id"),
            {"holder": human["id"], "claim_id": claim["id"]},
        )

    response = await disable(client, admin_key, human["id"])
    assert response.status_code == 200, response.text

    claim_now = await get(client, admin_key, f"claims/{claim['id']}")
    assert (claim_now["status"], claim_now["releaseReason"]) == ("released", "principal_disabled")
    assert (await get(client, admin_key, f"tasks/{task['id']}"))["status"] == "todo"
    # The session is someone else's: it stays open.
    assert (await get(client, admin_key, f"sessions/{work_session['id']}"))["status"] == "active"
    event = (await events_of(client, admin_key, "principal", human["id"]))[-1]
    assert (event["payload"]["closedSessions"], event["payload"]["releasedClaims"]) == (0, 1)


async def test_a_failed_child_run_leaves_its_parent_a_result(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, parent_key = await create_agent_with_key(client, admin_key)
    child_agent, child_key = await create_agent_with_key(client, admin_key, name="child-agent")
    task = await create_task(client, admin_key)
    _, parent_run = await claim_and_run(client, parent_key, task["id"])
    handle = (await launch(client, parent_key, parent_run["id"])).json()["childHandle"]
    _, child_run = await claim_and_run(client, child_key, handle["childTaskId"])

    response = await disable(client, admin_key, child_agent["id"])
    assert response.status_code == 200, response.text

    child_now = await get(client, admin_key, f"runs/{child_run['id']}")
    assert (child_now["status"], child_now["failureReason"]) == ("failed", "principal_disabled")
    resolved = (await get(client, parent_key, f"child-handles/{handle['id']}"))["childHandle"]
    assert resolved["status"] == "failed"
    assert (resolved["result"]["outcome"], resolved["result"]["summary"]) == (
        "failed",
        "principal_disabled",
    )
    # The parent's own run is not the disabled principal's: it keeps running.
    assert (await get(client, admin_key, f"runs/{parent_run['id']}"))["status"] == "running"


async def test_delegations_and_sessions_on_behalf_of_a_disabled_human_go(
    client: httpx.AsyncClient,
) -> None:
    """An agent working for the human through someone else's session loses that work."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    human, _ = await create_agent_with_key(client, admin_key, name="alice", kind="human")
    agent, agent_key = await create_agent_with_key(client, admin_key)
    created = await client.post(
        "/api/v1/delegations",
        json={
            "humanPrincipalId": human["id"],
            "agentPrincipalId": agent["id"],
            "permissions": ["tasks.write"],
        },
        headers=auth(admin_key),
    )
    assert created.status_code == 201, created.text
    delegation = created.json()
    on_behalf = await open_work_session(client, agent_key, onBehalfOf=human["id"])
    own = await open_work_session(client, agent_key, client_name="own")
    task, other_task = await create_task(client, admin_key), await create_task(client, admin_key)
    claim = (await claim_task(client, agent_key, task["id"], on_behalf["id"])).json()
    other_claim = (await claim_task(client, agent_key, other_task["id"], own["id"])).json()

    response = await disable(client, admin_key, human["id"])
    assert response.status_code == 200, response.text

    listed = await get(client, admin_key, "delegations")
    assert [d["revokedAt"] is not None for d in listed["items"]] == [True]
    revoked = (await events_of(client, admin_key, "delegation", delegation["id"]))[-1]
    assert (revoked["type"], revoked["payload"]) == (
        "delegation.revoked",
        {"reason": "principal_disabled"},
    )
    assert (await get(client, admin_key, f"sessions/{on_behalf['id']}"))["status"] == "closed"
    claim_now = await get(client, admin_key, f"claims/{claim['id']}")
    assert (claim_now["status"], claim_now["releaseReason"]) == ("released", "principal_disabled")
    event = (await events_of(client, admin_key, "principal", human["id"]))[-1]
    assert event["payload"]["revokedDelegations"] == 1
    assert event["payload"]["closedSessions"] == 1
    assert event["payload"]["releasedClaims"] == 1

    # The agent itself keeps its own session, its own work and its entry.
    assert (await get(client, agent_key, f"sessions/{own['id']}"))["status"] == "active"
    assert (await get(client, admin_key, f"claims/{other_claim['id']}"))["status"] == "active"


async def test_only_an_admin_disables_an_admin(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, manager_key = await create_agent_with_key(
        client, admin_key, name="manager", permissions=["principals.read", "principals.write"]
    )
    admin_by_key, _ = await create_agent_with_key(
        client, admin_key, name="second admin", kind="human", permissions=["admin"]
    )
    admin_by_binding = await create_principal(client, admin_key, kind="human")
    bound = await upsert(
        client,
        admin_key,
        admin_by_binding["id"],
        permissions=["admin"],
        issuer=ISSUER,
        iamTenantId=str(uuid.uuid4()),
        iamPrincipalId=str(uuid.uuid4()),
    )
    assert bound.status_code == 201, bound.text
    former_admin = await create_principal(client, admin_key, kind="human")
    former_key = await client.post(
        f"/api/v1/principals/{former_admin['id']}/api-keys",
        json={"permissions": ["admin"]},
        headers=auth(admin_key),
    )
    assert former_key.status_code == 201, former_key.text
    revoked = await client.post(
        f"/api/v1/api-keys/{former_key.json()['id']}:revoke", headers=auth(admin_key)
    )
    assert revoked.status_code == 200, revoked.text
    # An expired key does not authenticate either: the rule of ``:enable``.
    lapsed_admin = await create_principal(client, admin_key, kind="human")
    lapsed_key = await client.post(
        f"/api/v1/principals/{lapsed_admin['id']}/api-keys",
        json={
            "permissions": ["admin"],
            "expiresAt": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        },
        headers=auth(admin_key),
    )
    assert lapsed_key.status_code == 201, lapsed_key.text
    plain = await create_principal(client, admin_key, kind="human")

    for target in (admin_by_key, admin_by_binding):
        refused = await disable(client, manager_key, target["id"])
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] == "permission_escalation"
        assert (await get(client, admin_key, f"principals/{target['id']}"))["status"] == "active"

    # A revoked or expired admin key is no admin any more; a plain human never was.
    for target in (former_admin, lapsed_admin, plain):
        allowed = await disable(client, manager_key, target["id"])
        assert allowed.status_code == 200, allowed.text

    by_admin = await disable(client, admin_key, admin_by_key["id"])
    assert by_admin.status_code == 200, by_admin.text


async def test_a_replay_under_the_same_idempotency_key_changes_nothing(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    agent, _ = await create_agent_with_key(client, admin_key)
    headers = {**auth(admin_key), "Idempotency-Key": "disable-agent-1"}
    path = f"/api/v1/principals/{agent['id']}:disable"

    first = await client.post(path, json={"reason": "gone"}, headers=headers)
    assert first.status_code == 200, first.text
    events = await events_of(client, admin_key, "principal", agent["id"])

    replay = await client.post(path, json={"reason": "gone"}, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert await events_of(client, admin_key, "principal", agent["id"]) == events

    reused = await client.post(path, json={"reason": "other"}, headers=headers)
    assert reused.status_code == 409
    assert await events_of(client, admin_key, "principal", agent["id"]) == events


async def test_a_registered_agent_leaves_through_retire(client: httpx.AsyncClient) -> None:
    admin_key, workspace = await _tenant(client)
    assert (await _publish(client, admin_key, coder_spec(workspace["id"]))).status_code == 201
    principal_id = (await _link(client, admin_key)).json()["principalId"]

    refused = await disable(client, admin_key, principal_id)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "use_agent_retire"
    assert refused.json()["error"]["details"]["agent"] == "coder"

    # An agent holding admin: a caller without admin is refused before the
    # agent check, so the refusal does not name the agent.
    admin_grant = await client.post(
        f"/api/v1/principals/{principal_id}/api-keys",
        json={"permissions": ["admin"]},
        headers=auth(admin_key),
    )
    assert admin_grant.status_code == 201, admin_grant.text
    _, manager_key = await create_agent_with_key(
        client, admin_key, name="manager", permissions=["principals.read", "principals.write"]
    )
    escalation = await disable(client, manager_key, principal_id)
    assert escalation.status_code == 403, escalation.text
    assert escalation.json()["error"]["code"] == "permission_escalation"
    assert "agent" not in escalation.json()["error"]["details"]

    retired = await client.post(
        "/api/v1/agents/coder:retire", json={"reason": "done"}, headers=auth(admin_key)
    )
    assert retired.status_code == 200, retired.text
    # Retirement disabled the principal: a disable after it is a no-op.
    again = await disable(client, admin_key, principal_id)
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "disabled"


async def test_its_skill_calls_and_leases_are_withdrawn(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    caller, caller_key = await create_agent_with_key(
        client, admin_key, name="caller", permissions=CALLER_PERMISSIONS
    )
    executor, executor_key = await create_agent_with_key(
        client, admin_key, name="executor", permissions=EXECUTOR_PERMISSIONS
    )
    await published(client, admin_key, retryPolicy={"maxAttempts": 2, "backoffSeconds": 0})
    leased = (await invoke(client, caller_key, "repo.search", {"query": "x"})).json()
    waiting = (await invoke(client, caller_key, "repo.search", {"query": "y"})).json()
    lease = (await claim(client, executor_key)).json()["invocation"]
    assert lease["id"] == leased["id"]

    # The executor goes: its lease returns to the queue, the attempt spent.
    response = await disable(client, admin_key, executor["id"])
    assert response.status_code == 200, response.text
    returned = await get(client, admin_key, f"skill-invocations/{leased['id']}")
    assert (returned["status"], returned["attempt"]) == ("pending", 1)
    assert returned["error"]["code"] == "principal_disabled"
    event = (await events_of(client, admin_key, "principal", executor["id"]))[-1]
    assert event["payload"]["withdrawnInvocations"] == 1

    # The caller goes: nothing runs on its authority any more.
    response = await disable(client, admin_key, caller["id"])
    assert response.status_code == 200, response.text
    for invocation in (leased, waiting):
        gone = await get(client, admin_key, f"skill-invocations/{invocation['id']}")
        assert gone["status"] == "cancelled"
        assert (gone["error"]["code"], gone["error"]["message"]) == (
            "basis_revoked",
            "principal_disabled",
        )
    event = (await events_of(client, admin_key, "principal", caller["id"]))[-1]
    assert event["payload"]["withdrawnInvocations"] == 2
