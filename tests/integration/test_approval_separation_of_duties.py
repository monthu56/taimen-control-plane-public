"""Separation of duties on an approval (CP-ADR-0074 §7, process-packages P008).

The core, not the process engine, refuses the decision of an excluded
principal: the refusal sits on the one decision path every entry point goes
through, so the ordinary ``:approve`` route is where it is tested.
"""

import uuid
from typing import Any

import httpx

from tests.helpers import (
    assign_role,
    auth,
    create_agent_with_key,
    create_role,
    create_task,
    do_bootstrap,
)

DECIDER_PERMISSIONS = ["approvals.read", "approvals.decide", "approvals.manage", "tasks.read"]


async def _world(client: httpx.AsyncClient) -> dict[str, Any]:
    """An initiator and a colleague who both hold the approving role."""
    admin = (await do_bootstrap(client))["apiKey"]["key"]
    role = await create_role(client, admin, "approver")
    initiator, initiator_key = await create_agent_with_key(
        client, admin, name="initiator", permissions=DECIDER_PERMISSIONS, kind="human"
    )
    colleague, colleague_key = await create_agent_with_key(
        client, admin, name="colleague", permissions=DECIDER_PERMISSIONS, kind="human"
    )
    for principal in (initiator, colleague):
        await assign_role(client, admin, principal["id"], role["id"])
    return {
        "admin": admin,
        "role": role["id"],
        "initiator": initiator["id"],
        "initiator_key": initiator_key,
        "colleague": colleague["id"],
        "colleague_key": colleague_key,
    }


async def _request(client: httpx.AsyncClient, key: str, **body: Any) -> httpx.Response:
    return await client.post("/api/v1/approvals", json=body, headers=auth(key))


async def test_the_vote_of_an_excluded_principal_is_refused_by_the_core(
    client: httpx.AsyncClient,
) -> None:
    w = await _world(client)
    task = await create_task(client, w["admin"])
    created = await _request(
        client,
        w["admin"],
        task=task["id"],
        requiredRoleId=w["role"],
        excludedPrincipals=[w["initiator"], w["initiator"]],
    )
    assert created.status_code == 201, created.text
    approval = created.json()
    assert approval["excludedPrincipals"] == [w["initiator"]]

    # Holding the required role does not help: the ordinary decision route
    # refuses both an approve and a reject, and the approval stays pending.
    for verb in ("approve", "reject"):
        refused = await client.post(
            f"/api/v1/approvals/{approval['id']}:{verb}",
            json={"comment": "my own request"},
            headers=auth(w["initiator_key"]),
        )
        assert refused.status_code == 403, refused.text
        error = refused.json()["error"]
        assert error["code"] == "separation_of_duties_violation"
        assert error["details"] == {"approvalId": approval["id"]}
    read = await client.get(f"/api/v1/approvals/{approval['id']}", headers=auth(w["admin"]))
    assert read.json()["status"] == "pending"
    assert read.json()["excludedPrincipals"] == [w["initiator"]]

    approved = await client.post(
        f"/api/v1/approvals/{approval['id']}:approve", json={}, headers=auth(w["colleague_key"])
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["decisionByPrincipalId"] == w["colleague"]

    events = (await client.get("/api/v1/events", headers=auth(w["admin"]))).json()["items"]
    requested = next(
        e for e in events if e["type"] == "approval.requested" and e["entityId"] == approval["id"]
    )
    assert requested["schemaVersion"] == 3
    assert requested["payload"]["excludedPrincipals"] == [w["initiator"]]
    assert not [
        e for e in events if e["type"] == "approval.rejected" and e["entityId"] == approval["id"]
    ]


async def test_an_excluded_principal_cannot_void_a_foreign_gate(
    client: httpx.AsyncClient,
) -> None:
    w = await _world(client)
    task = await create_task(client, w["admin"])
    gate = (
        await _request(
            client,
            w["admin"],
            task=task["id"],
            requiredRoleId=w["role"],
            gate=True,
            excludedPrincipals=[w["initiator"]],
        )
    ).json()

    refused = await client.post(
        f"/api/v1/approvals/{gate['id']}:cancel", json={}, headers=auth(w["initiator_key"])
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "separation_of_duties_violation"

    cancelled = await client.post(
        f"/api/v1/approvals/{gate['id']}:cancel", json={}, headers=auth(w["colleague_key"])
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"


async def test_an_excluded_principal_is_not_asked_to_decide(client: httpx.AsyncClient) -> None:
    w = await _world(client)
    task = await create_task(client, w["admin"])
    approval = (
        await _request(
            client,
            w["admin"],
            task=task["id"],
            requiredRoleId=w["role"],
            excludedPrincipals=[w["initiator"]],
        )
    ).json()

    async def listed(key: str) -> set[str]:
        response = await client.get("/api/v1/me/attention", headers=auth(key))
        assert response.status_code == 200, response.text
        return {item["entity"]["id"] for item in response.json()["items"]}

    assert approval["id"] not in await listed(w["initiator_key"])
    assert approval["id"] in await listed(w["colleague_key"])


async def test_the_request_is_checked(client: httpx.AsyncClient) -> None:
    w = await _world(client)
    task = await create_task(client, w["admin"])

    nobody_left = await _request(
        client,
        w["admin"],
        task=task["id"],
        assignedPrincipalId=w["colleague"],
        excludedPrincipals=[w["colleague"]],
    )
    assert nobody_left.status_code == 422, nobody_left.text
    assert nobody_left.json()["error"]["code"] == "invalid_approval"

    stranger = str(uuid.uuid4())
    unknown = await _request(
        client,
        w["admin"],
        task=task["id"],
        requiredRoleId=w["role"],
        excludedPrincipals=[w["initiator"], stranger],
    )
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["error"]["details"] == {"principalId": stranger}

    too_many = await _request(
        client,
        w["admin"],
        task=task["id"],
        requiredRoleId=w["role"],
        excludedPrincipals=[str(uuid.uuid4()) for _ in range(101)],
    )
    assert too_many.status_code == 400, too_many.text
    assert too_many.json()["error"]["code"] == "invalid_request"

    # An assigned approval excluding someone else is fine.
    assigned = await _request(
        client,
        w["admin"],
        task=task["id"],
        assignedPrincipalId=w["colleague"],
        excludedPrincipals=[w["initiator"]],
    )
    assert assigned.status_code == 201, assigned.text
