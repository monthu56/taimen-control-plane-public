"""An approval of a task lives in the task's workspace (CP-ADR-0068).

Whoever holds the required role in the task's workspace (or above it, or
tenant-wide) may decide; the ``approval.requested`` event and the role-holder
listing name that same workspace, so the addressees are exactly the deciders.
"""

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.worker.main import Worker
from tests.helpers import (
    assign_role,
    auth,
    create_agent_with_key,
    create_role,
    create_task,
    create_workspace,
    do_bootstrap,
)
from tests.integration.test_work_rules_m13 import DRIFT_RULE, worker  # noqa: F401

DECIDER = ["approvals.read", "approvals.decide"]
# The revision right before task approvals inherited the task's workspace.
BEFORE_BACKFILL = "c5e1a7d3f9b2"


async def _world(client: httpx.AsyncClient) -> dict[str, Any]:
    """Workspaces finance -> finance/invoices and sales, a role, a task in
    finance/invoices and a decider holding nothing yet."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    finance = await create_workspace(client, admin_key, "finance")
    invoices = await create_workspace(client, admin_key, "invoices", parent_id=finance["id"])
    sales = await create_workspace(client, admin_key, "sales")
    role = await create_role(client, admin_key, "finance-director")
    decider, decider_key = await create_agent_with_key(
        client, admin_key, name="director", permissions=DECIDER
    )
    task = await create_task(client, admin_key, title="Pay invoice", workspaceId=invoices["id"])
    return {
        "key": admin_key,
        "finance": finance,
        "invoices": invoices,
        "sales": sales,
        "role": role,
        "decider": decider,
        "deciderKey": decider_key,
        "task": task,
    }


async def _request(client: httpx.AsyncClient, key: str, **body: Any) -> httpx.Response:
    return await client.post("/api/v1/approvals", json=body, headers=auth(key))


async def _approve(client: httpx.AsyncClient, key: str, approval_id: str) -> httpx.Response:
    return await client.post(f"/api/v1/approvals/{approval_id}:approve", json={}, headers=auth(key))


async def _requested_event(client: httpx.AsyncClient, key: str, approval_id: str) -> dict[str, Any]:
    items = (
        await client.get(
            "/api/v1/events", params={"types": "approval.requested"}, headers=auth(key)
        )
    ).json()["items"]
    event: dict[str, Any] = next(e for e in items if e["entityId"] == approval_id)
    return event


async def _holders(
    client: httpx.AsyncClient, key: str, role_id: str, workspace_id: str
) -> set[str]:
    response = await client.get(
        f"/api/v1/roles/{role_id}/principals",
        params={"workspaceId": workspace_id},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    return {p["id"] for p in response.json()["items"]}


async def test_a_role_in_the_tasks_workspace_decides_and_is_the_addressee(
    client: httpx.AsyncClient,
) -> None:
    world = await _world(client)
    key, role, invoices = world["key"], world["role"], world["invoices"]
    await assign_role(client, key, world["decider"]["id"], role["id"], workspace_id=invoices["id"])

    created = await _request(client, key, task=world["task"]["id"], requiredRoleId=role["id"])
    assert created.status_code == 201, created.text
    approval = created.json()
    assert approval["workspaceId"] == invoices["id"]

    # The event, the projection and the holder listing agree on the workspace.
    event = await _requested_event(client, key, approval["id"])
    assert event["payload"]["workspaceId"] == invoices["id"]
    assert event["workspaceId"] == invoices["id"]
    holders = await _holders(client, key, role["id"], event["payload"]["workspaceId"])
    assert world["decider"]["id"] in holders

    decided = await _approve(client, world["deciderKey"], approval["id"])
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"


async def test_a_role_in_another_workspace_is_not_eligible(client: httpx.AsyncClient) -> None:
    world = await _world(client)
    key, role = world["key"], world["role"]
    await assign_role(
        client, key, world["decider"]["id"], role["id"], workspace_id=world["sales"]["id"]
    )
    approval = (
        await _request(client, key, task=world["task"]["id"], requiredRoleId=role["id"])
    ).json()

    assert world["decider"]["id"] not in await _holders(
        client, key, role["id"], world["invoices"]["id"]
    )
    refused = await _approve(client, world["deciderKey"], approval["id"])
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "not_eligible"


async def test_a_tenant_wide_role_decides(client: httpx.AsyncClient) -> None:
    world = await _world(client)
    key, role = world["key"], world["role"]
    await assign_role(client, key, world["decider"]["id"], role["id"])
    approval = (
        await _request(client, key, task=world["task"]["id"], requiredRoleId=role["id"])
    ).json()

    decided = await _approve(client, world["deciderKey"], approval["id"])
    assert decided.status_code == 200, decided.text


async def test_an_explicit_workspace_may_only_widen_the_tasks_one(
    client: httpx.AsyncClient,
) -> None:
    world = await _world(client)
    key, role, task = world["key"], world["role"], world["task"]

    # An ancestor of the task's workspace widens who may decide: kept as given.
    wider = await _request(
        client, key, task=task["id"], requiredRoleId=role["id"], workspaceId=world["finance"]["id"]
    )
    assert wider.status_code == 201, wider.text
    assert wider.json()["workspaceId"] == world["finance"]["id"]

    # A foreign workspace would hand the decision to people outside the task.
    foreign = await _request(
        client, key, task=task["id"], requiredRoleId=role["id"], workspaceId=world["sales"]["id"]
    )
    assert foreign.status_code == 422, foreign.text
    assert foreign.json()["error"]["code"] == "invalid_approval"

    # A tenant-level task has no workspace to narrow into.
    tenant_task = await create_task(client, key, title="Tenant work")
    narrowed = await _request(
        client,
        key,
        task=tenant_task["id"],
        requiredRoleId=role["id"],
        workspaceId=world["finance"]["id"],
    )
    assert narrowed.status_code == 422, narrowed.text
    tenant_level = await _request(client, key, task=tenant_task["id"], requiredRoleId=role["id"])
    assert tenant_level.status_code == 201, tenant_level.text
    assert tenant_level.json()["workspaceId"] is None


async def test_request_decision_rule_asks_in_the_tasks_workspace(
    client: httpx.AsyncClient,
    worker: Worker,  # noqa: F811
) -> None:
    world = await _world(client)
    key, role, finance = world["key"], world["role"], world["finance"]
    await assign_role(client, key, world["decider"]["id"], role["id"], workspace_id=finance["id"])
    rule_body = {
        **DRIFT_RULE,
        "workspaceId": finance["id"],
        "action": {
            **DRIFT_RULE["action"],
            "kind": "request_decision",
            "fields": {**DRIFT_RULE["action"]["fields"], "approverRole": role["id"]},
        },
    }
    rule = await client.post("/api/v1/rules", json=rule_body, headers=auth(key))
    assert rule.status_code == 201, rule.text
    observed = await client.post(
        "/api/v1/observations",
        json={"kind": "drift.seen", "content": "seen", "data": {"id": "a", "severity": "high"}},
        headers=auth(key),
    )
    assert observed.status_code in (200, 201), observed.text
    await worker.run_once()

    evaluations = (
        await client.get(f"/api/v1/rules/{rule.json()['id']}/evaluations", headers=auth(key))
    ).json()["items"]
    approval_id = evaluations[0]["result"]["work"][0]["approvalId"]
    approval = (await client.get(f"/api/v1/approvals/{approval_id}", headers=auth(key))).json()
    assert approval["workspaceId"] == finance["id"]
    decided = await _approve(client, world["deciderKey"], approval_id)
    assert decided.status_code == 200, decided.text


@pytest.fixture
def backfill_config(migrated_database: str) -> Iterator[Config]:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrated_database.replace("+psycopg", ""))
    yield config
    alembic_command.upgrade(config, "head")


async def test_migration_gives_pending_task_approvals_the_tasks_workspace(
    client: httpx.AsyncClient, sync_engine: Engine, backfill_config: Config
) -> None:
    world = await _world(client)
    key, role, task = world["key"], world["role"], world["task"]
    pending = (await _request(client, key, task=task["id"], requiredRoleId=role["id"])).json()
    decided = (
        await _request(client, key, task=task["id"], assignedPrincipalId=world["decider"]["id"])
    ).json()
    assert (await _approve(client, world["deciderKey"], decided["id"])).status_code == 200
    no_task = (await _request(client, key, requiredRoleId=role["id"])).json()

    alembic_command.downgrade(backfill_config, BEFORE_BACKFILL)
    # What the old code wrote: no workspace on any of them.
    with sync_engine.begin() as connection:
        connection.execute(text("UPDATE approvals SET workspace_id = NULL"))
    alembic_command.upgrade(backfill_config, "head")

    with sync_engine.connect() as connection:
        result = connection.execute(text("SELECT id::text, workspace_id FROM approvals"))
        rows: dict[str, Any] = {row[0]: row[1] for row in result}
    assert str(rows[pending["id"]]) == world["invoices"]["id"]
    assert rows[decided["id"]] is None
    assert rows[no_task["id"]] is None
