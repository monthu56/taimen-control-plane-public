"""Reading the journal as a subscription (CP-ADR-0068, notifications N002).

A consumer narrows the journal by type prefixes and by workspace subtree,
``events.read`` is asked on that workspace, every event carries its workspace
and the version of its payload schema, ``approval.*`` payloads say enough to
tell a person what to decide, and the holders of a role are listable as the
addressees of such a decision.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from platform_auth import ObjectPage, PolicyDecision
from starlette.websockets import WebSocketDisconnect

from control_plane.application.authorization import AuthContext, Authorizer, configure_authorizer
from control_plane.application.queries.events import list_events
from control_plane.config import Settings
from control_plane.domain.errors import AuthorizationError
from control_plane.main import create_app
from tests.event_contract import payload_violations
from tests.helpers import (
    BOOTSTRAP_TOKEN,
    assign_role,
    auth,
    create_agent_with_key,
    create_role,
    create_task,
    create_workspace,
    do_bootstrap,
)

DECIDER = ["approvals.read", "approvals.decide"]


async def _events(client: httpx.AsyncClient, key: str, **params: Any) -> dict[str, Any]:
    response = await client.get("/api/v1/events", params=params, headers=auth(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _request_approval(client: httpx.AsyncClient, key: str, **body: Any) -> dict[str, Any]:
    response = await client.post("/api/v1/approvals", json=body, headers=auth(key))
    assert response.status_code == 201, response.text
    approval: dict[str, Any] = response.json()
    return approval


async def _two_trees(client: httpx.AsyncClient) -> dict[str, Any]:
    """Tenant with workspaces ops -> ops/child and sales; a task and an approval
    in each of ops/child and sales, plus tenant-level noise."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    ops = await create_workspace(client, admin_key, "ops")
    child = await create_workspace(client, admin_key, "ops-child", parent_id=ops["id"])
    sales = await create_workspace(client, admin_key, "sales")
    reviewer, _ = await create_agent_with_key(client, admin_key, name="reviewer")
    in_child = await create_task(client, admin_key, title="Child work", workspaceId=child["id"])
    in_sales = await create_task(client, admin_key, title="Sales work", workspaceId=sales["id"])
    child_approval = await _request_approval(
        client, admin_key, task=in_child["id"], assignedPrincipalId=reviewer["id"]
    )
    sales_approval = await _request_approval(
        client, admin_key, task=in_sales["id"], assignedPrincipalId=reviewer["id"]
    )
    return {
        "key": admin_key,
        "tenantId": boot["tenant"]["id"],
        "adminId": boot["adminPrincipal"]["id"],
        "ops": ops,
        "child": child,
        "sales": sales,
        "childTask": in_child,
        "salesTask": in_sales,
        "childApproval": child_approval,
        "salesApproval": sales_approval,
    }


# --- filters -----------------------------------------------------------------


async def test_type_prefixes_return_only_matching_events_in_order(
    client: httpx.AsyncClient,
) -> None:
    world = await _two_trees(client)
    everything = (await _events(client, world["key"]))["items"]
    page = await _events(client, world["key"], types="approval.")
    types = [e["type"] for e in page["items"]]
    assert types == ["approval.requested", "approval.requested"]
    # Same relative order as the unfiltered journal.
    expected = [e["id"] for e in everything if e["type"].startswith("approval.")]
    assert [e["id"] for e in page["items"]] == expected

    # Repeated and comma-separated values combine; an exact type is a prefix too.
    both = await client.get(
        "/api/v1/events?types=approval.requested,workspace.&types=task.created",
        headers=auth(world["key"]),
    )
    assert {e["type"] for e in both.json()["items"]} == {
        "approval.requested",
        "workspace.created",
        "task.created",
    }


async def test_type_prefix_is_literal_not_a_pattern(client: httpx.AsyncClient) -> None:
    # "_" is a LIKE wildcard: "task_" must not match "task.created".
    world = await _two_trees(client)
    page = await _events(client, world["key"], types="task_")
    assert page["items"] == []


@pytest.mark.parametrize("bad", ["Approval.", "approval.*", "", ".x", "a b"])
async def test_malformed_type_prefix_is_rejected(client: httpx.AsyncClient, bad: str) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await client.get(f"/api/v1/events?types={bad}", headers=auth(admin_key))
    if bad == "":
        assert response.status_code == 200  # an empty value is no filter
        return
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_event_type_filter"


async def test_workspace_filter_returns_the_subtree_only(client: httpx.AsyncClient) -> None:
    world = await _two_trees(client)
    page = await _events(client, world["key"], workspaceId=world["ops"]["id"])
    items = page["items"]
    assert items, "the ops subtree has events"
    assert {e["workspaceId"] for e in items} <= {world["ops"]["id"], world["child"]["id"]}
    ids = {e["entityId"] for e in items}
    assert world["childTask"]["id"] in ids
    assert world["childApproval"]["id"] in ids
    assert world["salesTask"]["id"] not in ids
    assert world["salesApproval"]["id"] not in ids
    # Tenant-level events (bootstrap, principals) belong to no workspace.
    assert "tenant.bootstrapped" not in {e["type"] for e in items}

    only_child = await _events(
        client, world["key"], workspaceId=world["child"]["id"], types="approval."
    )
    assert [e["entityId"] for e in only_child["items"]] == [world["childApproval"]["id"]]


async def test_filtered_cursor_resumes_without_rescanning_or_losing(
    client: httpx.AsyncClient,
) -> None:
    world = await _two_trees(client)
    first = await _events(client, world["key"], types="approval.", workspaceId=world["ops"]["id"])
    assert len(first["items"]) == 1
    cursor = first["nextCursor"]

    # Unrelated events move the journal on; the filtered reader sees nothing new
    # but its cursor still moves past them.
    await create_task(client, world["key"], title="noise", workspaceId=world["sales"]["id"])
    idle = await _events(
        client, world["key"], types="approval.", workspaceId=world["ops"]["id"], cursor=cursor
    )
    assert idle["items"] == []
    assert idle["hasMore"] is False
    assert idle["nextCursor"] != cursor

    # A matching event after that is delivered from the advanced cursor.
    reviewer, _ = await create_agent_with_key(client, world["key"], name="reviewer-2")
    later = await _request_approval(
        client, world["key"], task=world["childTask"]["id"], assignedPrincipalId=reviewer["id"]
    )
    resumed = await _events(
        client,
        world["key"],
        types="approval.",
        workspaceId=world["ops"]["id"],
        cursor=idle["nextCursor"],
    )
    assert [e["entityId"] for e in resumed["items"]] == [later["id"]]


async def test_filters_page_with_has_more(client: httpx.AsyncClient) -> None:
    world = await _two_trees(client)
    for n in range(3):
        await create_task(client, world["key"], title=f"t{n}", workspaceId=world["child"]["id"])
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"types": "task.created", "workspaceId": world["ops"]["id"]}
        params["limit"] = 2
        if cursor:
            params["cursor"] = cursor
        page = await _events(client, world["key"], **params)
        seen += [e["payload"]["title"] for e in page["items"]]
        cursor = page["nextCursor"]
        if not page["hasMore"]:
            break
    assert seen == ["Child work", "t0", "t1", "t2"]


# --- rights ------------------------------------------------------------------


async def test_workspace_filter_requires_events_read(client: httpx.AsyncClient) -> None:
    world = await _two_trees(client)
    _, no_events = await create_agent_with_key(
        client, world["key"], name="no-events", permissions=["tasks.read"]
    )
    response = await client.get(
        "/api/v1/events", params={"workspaceId": world["ops"]["id"]}, headers=auth(no_events)
    )
    assert response.status_code == 403

    unknown = await client.get(
        "/api/v1/events", params={"workspaceId": str(uuid.uuid4())}, headers=auth(world["key"])
    )
    assert unknown.status_code == 404


class _WorkspaceEventsPolicy:
    """A PDP granting ``events.read`` on the listed workspaces only."""

    def __init__(self, readable: set[str]) -> None:
        self.readable = readable
        self.asked: list[str] = []

    async def check(  # type: ignore[no-untyped-def]
        self, ctx, action, resource, *, contextual=(), on_behalf_of=None, consistency="default"
    ):
        self.asked.append(f"{action}@{resource.key}")
        allowed = action == "events.read" and resource.key in self.readable
        return PolicyDecision(
            allowed=allowed,
            reason_code="allowed" if allowed else "denied_no_binding",
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


async def test_policy_decides_events_read_on_the_requested_workspace(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    world = await _two_trees(client)
    policy = _WorkspaceEventsPolicy({f"workspace:{world['ops']['id']}"})
    configure_authorizer(Authorizer(policy, "policy"))
    try:
        ctx = AuthContext(
            tenant_id=uuid.UUID(world["tenantId"]),
            principal_id=uuid.UUID(world["adminId"]),
            principal_kind="service",
            api_key_id=uuid.uuid4(),
            permissions=frozenset(),
            iam_principal_id=uuid.uuid4(),
        )
        async with app.state.session_factory() as session:
            page = await list_events(
                session, ctx, workspace_id=uuid.UUID(world["ops"]["id"]), types=("approval.",)
            )
            assert [e.entity_id for e in page.events] == [uuid.UUID(world["childApproval"]["id"])]
            with pytest.raises(AuthorizationError):
                await list_events(session, ctx, workspace_id=uuid.UUID(world["sales"]["id"]))
            # Tenant-wide reading is a separate grant the consumer does not have.
            with pytest.raises(AuthorizationError):
                await list_events(session, ctx)
    finally:
        configure_authorizer(Authorizer(None, "local"))
    assert f"events.read@workspace:{world['sales']['id']}" in policy.asked


# --- envelope and approval payload v2 ----------------------------------------


async def test_envelope_carries_workspace_and_schema_version(client: httpx.AsyncClient) -> None:
    world = await _two_trees(client)
    items = (await _events(client, world["key"]))["items"]
    by_entity = {(e["type"], e["entityId"]): e for e in items}
    task_created = by_entity[("task.created", world["childTask"]["id"])]
    assert task_created["workspaceId"] == world["child"]["id"]
    assert task_created["schemaVersion"] == 1
    requested = by_entity[("approval.requested", world["childApproval"]["id"])]
    # Requested without a workspace, the approval lives in the task's one.
    assert requested["workspaceId"] == world["child"]["id"]
    assert requested["schemaVersion"] == 3
    bootstrapped = next(e for e in items if e["type"] == "tenant.bootstrapped")
    assert bootstrapped["workspaceId"] is None
    workspace_created = by_entity[("workspace.created", world["sales"]["id"])]
    assert workspace_created["workspaceId"] == world["sales"]["id"]


async def test_approval_events_v2_payloads(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    me = boot["adminPrincipal"]
    ops = await create_workspace(client, admin_key, "ops")
    role = await create_role(client, admin_key, "approver")
    reviewer, reviewer_key = await create_agent_with_key(
        client, admin_key, name="reviewer", permissions=DECIDER
    )
    await assign_role(client, admin_key, reviewer["id"], role["id"], workspace_id=ops["id"])
    task = await create_task(client, admin_key, title="Ship the release", workspaceId=ops["id"])
    secret = "token=" + "A" * 40
    long_comment = "please look " + secret + " " + "x" * 2000

    first = await _request_approval(
        client,
        admin_key,
        task=task["id"],
        requiredRoleId=role["id"],
        workspaceId=ops["id"],
        comment=long_comment,
        gate=True,
    )
    decided = await client.post(
        f"/api/v1/approvals/{first['id']}:approve",
        json={"comment": "ok " + secret},
        headers=auth(reviewer_key),
    )
    assert decided.status_code == 200, decided.text
    second = await _request_approval(
        client, admin_key, task=task["id"], requiredRoleId=role["id"], workspaceId=ops["id"]
    )
    rejected = await client.post(
        f"/api/v1/approvals/{second['id']}:reject", headers=auth(reviewer_key)
    )
    assert rejected.status_code == 200, rejected.text
    third = await _request_approval(
        client, admin_key, requiredRoleId=role["id"], comment="no task here"
    )
    cancelled = await client.post(
        f"/api/v1/approvals/{third['id']}:cancel", json={}, headers=auth(admin_key)
    )
    assert cancelled.status_code == 200, cancelled.text

    events = (await _events(client, admin_key, types="approval."))["items"]
    by = {(e["type"], e["entityId"]): e for e in events}

    requested = by[("approval.requested", first["id"])]["payload"]
    assert requested["workspaceId"] == ops["id"]
    assert requested["taskPublicId"] == task["publicId"]
    assert requested["taskTitle"] == "Ship the release"
    assert requested["requestedBy"] == me["id"]
    assert requested["gate"] is True
    assert requested["requiredRoleId"] == role["id"]
    assert len(requested["comment"]) == 1000
    assert secret not in requested["comment"]
    assert "[redacted]" in requested["comment"]
    assert requested["excludedPrincipals"] == []

    approved = by[("approval.approved", first["id"])]["payload"]
    assert approved["decisionBy"] == reviewer["id"]
    assert approved["comment"] == "ok [redacted]"
    assert approved["channel"] is None
    assert approved["taskId"] == task["id"]

    rejected_payload = by[("approval.rejected", second["id"])]["payload"]
    assert rejected_payload["decisionBy"] == reviewer["id"]
    assert rejected_payload["comment"] is None

    no_task = by[("approval.requested", third["id"])]["payload"]
    assert no_task["taskPublicId"] is None
    assert no_task["taskTitle"] is None
    assert no_task["workspaceId"] is None
    assert by[("approval.cancelled", third["id"])]["payload"]["cancelledBy"] == me["id"]

    # Contract: every approval event validates against its catalog version
    # (approval.requested is at v3, CP-ADR-0074 §7; the others at v2).
    for event in events:
        version = 3 if event["type"] == "approval.requested" else 2
        assert event["schemaVersion"] == version
        assert payload_violations(event["type"], version, event["payload"]) == []
    # Versions only add fields: an older consumer's schema accepts the same payloads.
    for event in events:
        assert payload_violations(event["type"], 1, event["payload"]) == []
        assert payload_violations(event["type"], 2, event["payload"]) == []


# --- WebSocket ---------------------------------------------------------------


@pytest.fixture
def tc(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def _sync_setup(tc: TestClient) -> dict[str, str]:
    admin_key = tc.post(
        "/api/v1/bootstrap",
        json={"tenantSlug": "acme", "tenantName": "Acme", "adminDisplayName": "A"},
        headers=auth(BOOTSTRAP_TOKEN),
    ).json()["apiKey"]["key"]
    ops = tc.post(
        "/api/v1/workspaces", json={"slug": "ops", "name": "Ops"}, headers=auth(admin_key)
    ).json()
    sales = tc.post(
        "/api/v1/workspaces", json={"slug": "sales", "name": "Sales"}, headers=auth(admin_key)
    ).json()
    return {"key": admin_key, "ops": ops["id"], "sales": sales["id"]}


def test_ws_delivers_only_matching_events(tc: TestClient) -> None:
    with tc:
        world = _sync_setup(tc)
        key = world["key"]
        url = f"/api/v1/events/ws?types=task.&workspaceId={world['ops']}"
        with tc.websocket_connect(url, headers=auth(key)) as ws:
            # Noise first: another workspace, another type.
            tc.post(
                "/api/v1/tasks",
                json={"title": "sales", "workspaceId": world["sales"]},
                headers=auth(key),
            )
            tc.post(
                "/api/v1/workspaces",
                json={"slug": "ops-child", "name": "C", "parentId": world["ops"]},
                headers=auth(key),
            )
            tc.post(
                "/api/v1/tasks",
                json={"title": "ops", "workspaceId": world["ops"]},
                headers=auth(key),
            )
            first = ws.receive_json()
            assert (first["type"], first["payload"]["title"]) == ("task.created", "ops")
            assert first["workspaceId"] == world["ops"]
            assert first["schemaVersion"] == 1


def test_ws_refuses_a_workspace_it_may_not_read(tc: TestClient) -> None:
    with tc:
        world = _sync_setup(tc)
        principal = tc.post(
            "/api/v1/principals",
            json={"kind": "service", "displayName": "consumer"},
            headers=auth(world["key"]),
        ).json()
        no_events = tc.post(
            f"/api/v1/principals/{principal['id']}/api-keys",
            json={"permissions": ["tasks.read"]},
            headers=auth(world["key"]),
        ).json()["key"]

        with (
            tc.websocket_connect(
                f"/api/v1/events/ws?workspaceId={world['ops']}", headers=auth(no_events)
            ) as ws,
            pytest.raises(WebSocketDisconnect) as denied,
        ):
            ws.receive_json()
        assert denied.value.code == 4403

        with (
            tc.websocket_connect(
                f"/api/v1/events/ws?workspaceId={uuid.uuid4()}", headers=auth(world["key"])
            ) as ws,
            pytest.raises(WebSocketDisconnect) as missing,
        ):
            ws.receive_json()
        assert missing.value.code == 4404

        with (
            tc.websocket_connect("/api/v1/events/ws?types=BAD", headers=auth(world["key"])) as ws,
            pytest.raises(WebSocketDisconnect) as malformed,
        ):
            ws.receive_json()
        assert malformed.value.code == 4400


# --- role holders ------------------------------------------------------------


async def test_role_holders_in_a_workspace(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ops = await create_workspace(client, admin_key, "ops")
    child = await create_workspace(client, admin_key, "ops-child", parent_id=ops["id"])
    sales = await create_workspace(client, admin_key, "sales")
    role = await create_role(client, admin_key, "approver")
    other_role = await create_role(client, admin_key, "other")
    everywhere, _ = await create_agent_with_key(client, admin_key, name="everywhere")
    in_ops, _ = await create_agent_with_key(client, admin_key, name="in-ops")
    in_sales, _ = await create_agent_with_key(client, admin_key, name="in-sales")
    other, _ = await create_agent_with_key(client, admin_key, name="other")
    await assign_role(client, admin_key, everywhere["id"], role["id"])
    await assign_role(client, admin_key, in_ops["id"], role["id"], workspace_id=ops["id"])
    await assign_role(client, admin_key, in_ops["id"], role["id"], workspace_id=child["id"])
    await assign_role(client, admin_key, in_sales["id"], role["id"], workspace_id=sales["id"])
    await assign_role(client, admin_key, other["id"], other_role["id"])

    async def holders(**params: str) -> list[str]:
        response = await client.get(
            f"/api/v1/roles/{role['id']}/principals", params=params, headers=auth(admin_key)
        )
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert all(set(item) == {"id", "kind", "displayName", "status"} for item in items)
        return sorted(item["displayName"] for item in items)

    # Ancestors count, a sibling subtree does not; one entry per principal.
    assert await holders(workspaceId=child["id"]) == ["everywhere", "in-ops"]
    assert await holders(workspaceId=sales["id"]) == ["everywhere", "in-sales"]
    # Without a workspace only tenant-wide holders — as for an approval without one.
    assert await holders() == ["everywhere"]

    missing = await client.get(f"/api/v1/roles/{uuid.uuid4()}/principals", headers=auth(admin_key))
    assert missing.status_code == 404
    unknown_ws = await client.get(
        f"/api/v1/roles/{role['id']}/principals",
        params={"workspaceId": str(uuid.uuid4())},
        headers=auth(admin_key),
    )
    assert unknown_ws.status_code == 404
    _, no_org = await create_agent_with_key(
        client, admin_key, name="no-org", permissions=["tasks.read"]
    )
    denied = await client.get(f"/api/v1/roles/{role['id']}/principals", headers=auth(no_org))
    assert denied.status_code == 403


async def test_role_holders_are_exactly_who_may_decide(client: httpx.AsyncClient) -> None:
    """The addressee list and the eligibility check are one rule."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ops = await create_workspace(client, admin_key, "ops")
    sales = await create_workspace(client, admin_key, "sales")
    role = await create_role(client, admin_key, "approver")
    in_ops, ops_key = await create_agent_with_key(
        client, admin_key, name="in-ops", permissions=DECIDER
    )
    in_sales, sales_key = await create_agent_with_key(
        client, admin_key, name="in-sales", permissions=DECIDER
    )
    await assign_role(client, admin_key, in_ops["id"], role["id"], workspace_id=ops["id"])
    await assign_role(client, admin_key, in_sales["id"], role["id"], workspace_id=sales["id"])
    approval = await _request_approval(
        client, admin_key, requiredRoleId=role["id"], workspaceId=ops["id"]
    )
    listed = (
        await client.get(
            f"/api/v1/roles/{role['id']}/principals",
            params={"workspaceId": ops["id"]},
            headers=auth(admin_key),
        )
    ).json()["items"]
    assert [p["id"] for p in listed] == [in_ops["id"]]
    assert (
        await client.post(f"/api/v1/approvals/{approval['id']}:approve", headers=auth(sales_key))
    ).status_code == 403
    assert (
        await client.post(f"/api/v1/approvals/{approval['id']}:approve", headers=auth(ops_key))
    ).status_code == 200
