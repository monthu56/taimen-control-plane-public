"""The observed state of a page of agents in one request (CP-ADR-0073, amendment 2026-09-29).

``GET /agents?include=status`` gives each item ``observedStatus``, the body of
``GET /agents/{key}/status``, read by the same query as the page: the console
pulse asks once per refresh, not once per agent.
"""

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from sqlalchemy import event

from tests.helpers import auth, create_agent_with_key, create_role, create_workspace, do_bootstrap

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agents"


def _fixture(name: str) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))
    return document


def _coder_spec(workspace: str) -> dict[str, Any]:
    spec = copy.deepcopy(_fixture("coder.yaml")["spec"])
    spec["work"]["workspace"] = workspace
    spec["work"]["taskTypes"] = ["task"]
    spec["workingCopy"]["review"]["reviewer"] = "reviewer"
    spec["skills"]["httpOrigins"] = ["https://cp.example.test"]
    return spec


async def _tenant(client: httpx.AsyncClient) -> tuple[str, str, dict[str, Any]]:
    """An admin key, a placement-service key and a workspace for the example spec."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "engineering")
    await create_role(client, admin_key, "coder")
    _, fleet_key = await create_agent_with_key(
        client,
        admin_key,
        name="fleet-controller",
        kind="service",
        permissions=["agents.read", "agents.status.write"],
    )
    return admin_key, fleet_key, workspace


async def _publish(client: httpx.AsyncClient, key: str, agent: str, spec: dict[str, Any]) -> None:
    response = await client.post(
        "/api/v1/agents", json={"key": agent, "spec": spec}, headers=auth(key)
    )
    assert response.status_code == 201, response.text


async def _report(
    client: httpx.AsyncClient, fleet_key: str, agent: str, **fields: Any
) -> dict[str, Any]:
    body = {"observedAt": "2026-09-29T10:00:00+00:00", **fields}
    response = await client.put(
        f"/api/v1/agents/{agent}/status", json=body, headers=auth(fleet_key)
    )
    assert response.status_code == 200, response.text
    reported: dict[str, Any] = response.json()
    return reported


async def _page(client: httpx.AsyncClient, key: str, **params: Any) -> dict[str, Any]:
    response = await client.get(
        "/api/v1/agents", params={"include": "status", **params}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    page: dict[str, Any] = response.json()
    return page


async def _one_by_one(client: httpx.AsyncClient, key: str, agent: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/agents/{agent}/status", headers=auth(key))
    assert response.status_code == 200, response.text
    observed: dict[str, Any] = response.json()
    return observed


@contextmanager
def _statements(app: FastAPI) -> Iterator[list[str]]:
    """Every SQL statement the application sends while the block runs."""
    seen: list[str] = []
    engine = app.state.engine.sync_engine

    def record(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def test_an_empty_registry_is_an_empty_page(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    assert await _page(client, admin_key) == {"items": [], "nextCursor": None}


async def test_each_item_carries_what_the_single_read_answers(client: httpx.AsyncClient) -> None:
    admin_key, fleet_key, workspace = await _tenant(client)
    await _publish(client, admin_key, "coder", _coder_spec(workspace["id"]))
    await _publish(client, admin_key, "fleet", _coder_spec(workspace["id"]))
    # placement: none — nothing ever reports it, the phase stays unknown (§4).
    await _publish(client, admin_key, "bridge", _fixture("process-bridge.yaml")["spec"])

    await _report(
        client,
        fleet_key,
        "coder",
        phase="waiting_for_node",
        reason={"code": "no_matching_node", "message": "no node has repos"},
        instances={"desired": 1, "ready": 0},
    )
    # Several instances of one agent are still one status row.
    await _report(
        client,
        fleet_key,
        "fleet",
        phase="running",
        node="node-a",
        observedRevision=1,
        instances={"desired": 3, "ready": 2},
    )

    page = await _page(client, admin_key)
    by_key = {item["key"]: item for item in page["items"]}
    assert set(by_key) == {"coder", "fleet", "bridge"}
    for key, item in by_key.items():
        assert item["observedStatus"] == await _one_by_one(client, admin_key, key), key
    assert by_key["bridge"]["observedStatus"]["phase"] == "unknown"
    assert by_key["bridge"]["observedStatus"]["instances"] is None
    assert by_key["fleet"]["observedStatus"]["instances"] == {"desired": 3, "ready": 2}
    assert by_key["coder"]["observedStatus"]["reason"]["code"] == "no_matching_node"

    # The rest of an item is the list item as before; without include there is no status.
    plain = await client.get("/api/v1/agents", headers=auth(admin_key))
    assert plain.status_code == 200, plain.text
    plain_items = plain.json()["items"]
    assert all("observedStatus" not in item for item in plain_items)
    assert [
        {k: v for k, v in item.items() if k != "observedStatus"} for item in page["items"]
    ] == plain_items

    # The filters of the list still apply.
    stopped = await client.patch(
        "/api/v1/agents/coder/state", json={"state": "stopped"}, headers=auth(admin_key)
    )
    assert stopped.status_code == 200, stopped.text
    running = await _page(client, admin_key, state="running", status="active")
    assert {item["key"] for item in running["items"]} == {"fleet", "bridge"}


async def test_pages_follow_the_cursor_of_the_list(client: httpx.AsyncClient) -> None:
    admin_key, fleet_key, workspace = await _tenant(client)
    keys = [f"agent-{i}" for i in range(5)]
    for i, key in enumerate(keys):
        await _publish(client, admin_key, key, _coder_spec(workspace["id"]))
        if i % 2 == 0:
            await _report(
                client, fleet_key, key, phase="running", instances={"desired": i, "ready": i}
            )

    seen: list[str] = []
    cursor: str | None = None
    while True:
        page = await _page(client, admin_key, limit=2, **({"cursor": cursor} if cursor else {}))
        assert len(page["items"]) <= 2
        for item in page["items"]:
            assert item["observedStatus"] == await _one_by_one(client, admin_key, item["key"])
        seen.extend(item["key"] for item in page["items"])
        cursor = page["nextCursor"]
        if cursor is None:
            break
    # Newest first, every agent exactly once — the order of the list.
    assert seen == list(reversed(keys))


async def test_one_query_for_the_page_whatever_its_size(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    admin_key, fleet_key, workspace = await _tenant(client)

    async def statements(**params: Any) -> int:
        await _page(client, admin_key, **params)  # warm whatever the request caches
        with _statements(app) as seen:
            await _page(client, admin_key, **params)
        return len(seen)

    await _publish(client, admin_key, "agent-0", _coder_spec(workspace["id"]))
    await _report(
        client, fleet_key, "agent-0", phase="running", instances={"desired": 1, "ready": 1}
    )
    one = await statements()

    for i in range(1, 6):
        await _publish(client, admin_key, f"agent-{i}", _coder_spec(workspace["id"]))
        await _report(
            client, fleet_key, f"agent-{i}", phase="running", instances={"desired": 1, "ready": 1}
        )
    six = await statements()
    assert six == one, "the status of each agent must not cost a query of its own"

    with _statements(app) as seen:
        await _page(client, admin_key)
    assert sum("agent_status" in statement for statement in seen) == 1


@pytest.mark.parametrize("include", ["statuses", "revision"])
async def test_an_unknown_include_is_refused(client: httpx.AsyncClient, include: str) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await client.get(
        "/api/v1/agents", params={"include": include}, headers=auth(admin_key)
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request"
