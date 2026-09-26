"""Task relations (graph + cycle prevention) and dependency readiness."""

import httpx
from sqlalchemy.engine import Engine

from tests.helpers import (
    auth,
    claim_task,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    make_tenant_directly,
    open_session,
)


async def _relate(
    client: httpx.AsyncClient, key: str, from_ref: str, to_ref: str, type_: str
) -> httpx.Response:
    return await client.post(
        f"/api/v1/tasks/{from_ref}/relations",
        json={"toTask": to_ref, "type": type_},
        headers=auth(key),
    )


async def test_relation_crud_and_constraints(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    a = await create_task(client, admin_key, title="A")
    b = await create_task(client, admin_key, title="B")

    response = await _relate(client, admin_key, a["id"], b["publicId"], "depends_on")
    assert response.status_code == 201, response.text
    relation = response.json()
    assert relation["fromTaskId"] == a["id"]
    assert relation["toTaskId"] == b["id"]

    # Duplicate -> 409; self-relation -> 422; bad type -> 422
    assert (await _relate(client, admin_key, a["id"], b["id"], "depends_on")).status_code == 409
    assert (await _relate(client, admin_key, a["id"], a["id"], "related_to")).status_code == 422
    assert (await _relate(client, admin_key, a["id"], b["id"], "loves")).status_code == 422

    # Both directions visible from either task
    for ref in (a["id"], b["id"]):
        items = (
            await client.get(f"/api/v1/tasks/{ref}/relations", headers=auth(admin_key))
        ).json()["items"]
        assert len(items) == 1

    # Remove; second delete -> 404
    assert (
        await client.delete(
            f"/api/v1/tasks/{a['id']}/relations/{relation['id']}", headers=auth(admin_key)
        )
    ).status_code == 204
    assert (
        await client.delete(
            f"/api/v1/tasks/{a['id']}/relations/{relation['id']}", headers=auth(admin_key)
        )
    ).status_code == 404


async def test_dependency_cycle_prevention(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    a = await create_task(client, admin_key, title="A")
    b = await create_task(client, admin_key, title="B")
    c = await create_task(client, admin_key, title="C")

    assert (await _relate(client, admin_key, a["id"], b["id"], "depends_on")).status_code == 201
    assert (await _relate(client, admin_key, b["id"], c["id"], "depends_on")).status_code == 201

    # Direct cycle
    response = await _relate(client, admin_key, b["id"], a["id"], "depends_on")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "dependency_cycle"
    # Transitive cycle
    assert (await _relate(client, admin_key, c["id"], a["id"], "depends_on")).status_code == 422
    # Mixed-type cycle: the needs graph already has A->B->C. "A blocks C"
    # means C needs A, which closes the loop -> rejected.
    assert (await _relate(client, admin_key, a["id"], c["id"], "blocks")).status_code == 422
    # "C blocks A" means A needs C — redundant but acyclic -> allowed.
    assert (await _relate(client, admin_key, c["id"], a["id"], "blocks")).status_code == 201

    # related_to never participates in cycles
    assert (await _relate(client, admin_key, c["id"], a["id"], "related_to")).status_code == 201

    # parent hierarchy has its own cycle check
    assert (await _relate(client, admin_key, a["id"], b["id"], "parent")).status_code == 201
    assert (await _relate(client, admin_key, b["id"], a["id"], "parent")).status_code == 422


async def test_cross_tenant_relations_rejected(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    a = await create_task(client, admin_key, title="A")

    _, key_b = make_tenant_directly(sync_engine, "tenant-b")
    b_task = await create_task(client, key_b, title="B in tenant B")

    # Tenant B cannot see A's task at all
    assert (await _relate(client, key_b, b_task["id"], a["id"], "depends_on")).status_code == 404
    # Tenant A cannot reference tenant B's task either
    assert (
        await _relate(client, admin_key, a["id"], b_task["id"], "depends_on")
    ).status_code == 404


async def test_dependency_readiness_gates_claim(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    session = await open_session(client, agent_key)

    dependent = await create_task(client, admin_key, title="Dependent")
    dep_b = await create_task(client, admin_key, title="Dep B")
    dep_c = await create_task(client, admin_key, title="Dep C")
    await _relate(client, admin_key, dependent["id"], dep_b["id"], "depends_on")
    # X blocks A: same readiness effect, opposite direction
    await _relate(client, admin_key, dep_c["id"], dependent["id"], "blocks")

    response = await claim_task(client, agent_key, dependent["id"], session["id"])
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "task_not_ready"
    blocked_by = {b["publicId"] for b in error["details"]["blockedBy"]}
    assert blocked_by == {dep_b["publicId"], dep_c["publicId"]}

    # Complete one prerequisite: still not ready
    await client.post(
        f"/api/v1/tasks/{dep_b['id']}:complete",
        headers={**auth(admin_key), "If-Match": '"task-1"'},
    )
    response = await claim_task(client, agent_key, dependent["id"], session["id"])
    assert response.status_code == 409
    assert len(response.json()["error"]["details"]["blockedBy"]) == 1

    # Complete the second: ready
    await client.post(
        f"/api/v1/tasks/{dep_c['id']}:complete",
        headers={**auth(admin_key), "If-Match": '"task-1"'},
    )
    assert (await claim_task(client, agent_key, dependent["id"], session["id"])).status_code == 200


async def test_cancelled_prerequisite_keeps_blocking(client: httpx.AsyncClient) -> None:
    """Deliberate: only 'done' satisfies a dependency; cancellation does not."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    session = await open_session(client, agent_key)

    dependent = await create_task(client, admin_key, title="Dependent")
    prerequisite = await create_task(client, admin_key, title="Prerequisite")
    relation = (
        await _relate(client, admin_key, dependent["id"], prerequisite["id"], "depends_on")
    ).json()

    await client.patch(
        f"/api/v1/tasks/{prerequisite['id']}",
        json={"status": "cancelled"},
        headers={**auth(admin_key), "If-Match": '"task-1"'},
    )
    assert (await claim_task(client, agent_key, dependent["id"], session["id"])).status_code == 409

    # Removing the relation unblocks
    await client.delete(
        f"/api/v1/tasks/{dependent['id']}/relations/{relation['id']}",
        headers=auth(admin_key),
    )
    assert (await claim_task(client, agent_key, dependent["id"], session["id"])).status_code == 200
