"""Skill invocations under concurrency (ADR-0056 §2)."""

import asyncio

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import auth, create_agent_with_key, do_bootstrap

CONTRACT = {
    "inputs": {"type": "object"},
    "outputs": {"type": "object"},
    "idempotency": "required",
    "implementation": {"protocol": "local", "entrypoint": "cp_skills.noop:run"},
}


async def _setup(client: httpx.AsyncClient) -> tuple[str, str]:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    response = await client.post(
        "/api/v1/skills",
        json={"name": "noop", "sideEffects": "none", "riskLevel": "low", "contract": CONTRACT},
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text
    _, executor_key = await create_agent_with_key(
        client, admin_key, name="executor", permissions=["skills.execute"]
    )
    return admin_key, executor_key


async def test_concurrent_invokes_with_one_key_create_one_invocation(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key, _ = await _setup(client)

    async def attempt() -> httpx.Response:
        return await client.post(
            "/api/v1/skills/noop:invoke",
            json={"inputs": {"a": 1}, "idempotencyKey": "same"},
            headers=auth(admin_key),
        )

    responses = await asyncio.gather(*[attempt() for _ in range(10)])
    assert all(r.status_code in (200, 201) for r in responses), [r.text for r in responses]
    assert len({r.json()["id"] for r in responses}) == 1
    with sync_engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM skill_invocations")).scalar_one()
        requested = conn.execute(
            text("SELECT count(*) FROM events WHERE event_type = 'skill.invocation_requested'")
        ).scalar_one()
    assert rows == 1
    assert requested == 1


async def test_concurrent_claims_hand_each_invocation_out_once(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key, executor_key = await _setup(client)
    for index in range(5):
        response = await client.post(
            "/api/v1/skills/noop:invoke",
            json={"inputs": {}, "idempotencyKey": f"k{index}"},
            headers=auth(admin_key),
        )
        assert response.status_code == 201

    async def attempt() -> httpx.Response:
        return await client.post(
            "/api/v1/skill-invocations:claim",
            json={"protocols": ["local"], "localEntrypoints": ["cp_skills.noop:run"]},
            headers=auth(executor_key),
        )

    responses = await asyncio.gather(*[attempt() for _ in range(12)])
    claimed = [r.json()["invocation"]["id"] for r in responses if r.status_code == 200]
    assert len(claimed) == 5
    assert len(set(claimed)) == 5
    assert sum(1 for r in responses if r.status_code == 204) == 7
    with sync_engine.connect() as conn:
        tokens = conn.execute(
            text("SELECT DISTINCT fencing_token FROM skill_invocations")
        ).scalars()
        assert list(tokens) == [1]


async def test_concurrent_invokes_with_one_approval_spend_it_once(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    response = await client.post(
        "/api/v1/skills",
        json={
            "name": "crm.update",
            "sideEffects": "external_write",
            "riskLevel": "high",
            "contract": {**CONTRACT, "idempotency": "none"},
        },
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text
    task = (
        await client.post("/api/v1/tasks", json={"title": "CRM"}, headers=auth(admin_key))
    ).json()
    approval = (
        await client.post(
            "/api/v1/approvals",
            json={
                "task": task["id"],
                "assignedPrincipalId": boot["adminPrincipal"]["id"],
                "gate": True,
            },
            headers=auth(admin_key),
        )
    ).json()
    decided = await client.post(
        f"/api/v1/approvals/{approval['id']}:approve", headers=auth(admin_key)
    )
    assert decided.status_code == 200, decided.text

    async def attempt(index: int) -> httpx.Response:
        return await client.post(
            "/api/v1/skills/crm.update:invoke",
            json={"inputs": {"n": index}, "taskId": task["id"], "approvalId": approval["id"]},
            headers=auth(admin_key),
        )

    responses = await asyncio.gather(*[attempt(index) for index in range(10)])
    codes = sorted(r.status_code for r in responses)
    assert codes == [201] + [409] * 9, [r.text for r in responses]
    assert {r.json()["error"]["code"] for r in responses if r.status_code == 409} == {
        "approval_already_used"
    }
    with sync_engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM skill_invocations")).scalar_one()
    assert rows == 1
