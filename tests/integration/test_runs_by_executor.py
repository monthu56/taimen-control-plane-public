"""Runs of one executor: ``principalId`` and ``agentKey`` on ``GET /runs``.

CP-ADR-0073, amendment of 2026-09-29: the agent card reads the latest runs of
its agent as one page, newest first by ``(startedAt, id)``, instead of sifting
the tenant's latest runs on the client.
"""

from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.common import utcnow
from tests.helpers import (
    auth,
    claim_task,
    create_agent_with_key,
    create_role,
    create_task,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
    open_session,
)
from tests.integration.test_agent_registry import _api_key, _link, _publish, coder_spec


async def _run(client: httpx.AsyncClient, admin_key: str, key: str, **start: Any) -> dict[str, Any]:
    """One finished run of ``key``'s principal on a task of its own."""
    session = await open_session(client, key)
    task = await create_task(client, admin_key, **start.pop("task", {}))
    claim = (await claim_task(client, key, task["id"], session["id"])).json()
    started = await client.post(
        f"/api/v1/tasks/{task['id']}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"], **start},
        headers=auth(key),
    )
    assert started.status_code == 201, started.text
    run: dict[str, Any] = started.json()
    failed = await client.post(
        f"/api/v1/runs/{run['id']}:fail", json={"failureReason": "done"}, headers=auth(key)
    )
    assert failed.status_code == 200, failed.text
    return run


async def _page(client: httpx.AsyncClient, key: str, **params: Any) -> dict[str, Any]:
    response = await client.get("/api/v1/runs", params=params, headers=auth(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _ids(page: dict[str, Any]) -> list[str]:
    return [item["id"] for item in page["items"]]


def _set_started(sync_engine: Engine, run_id: str, minutes_ago: int) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET started_at = :at WHERE id = :id"),
            {"at": utcnow() - timedelta(minutes=minutes_ago), "id": run_id},
        )


async def test_principal_filter_pages_newest_started_first(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    coder, coder_key = await create_agent_with_key(client, admin_key, name="coder")
    _, other_key = await create_agent_with_key(client, admin_key, name="other")

    runs = [await _run(client, admin_key, coder_key) for _ in range(3)]
    foreign = await _run(client, admin_key, other_key)
    # The order is by start, not by creation: the newest-created run started first.
    _set_started(sync_engine, runs[2]["id"], minutes_ago=30)
    expected = [runs[1]["id"], runs[0]["id"], runs[2]["id"]]

    page = await _page(client, admin_key, principalId=coder["id"])
    assert _ids(page) == expected
    assert page["nextCursor"] is None
    assert foreign["id"] not in _ids(page)
    assert {item["principalId"] for item in page["items"]} == {coder["id"]}

    # The unfiltered list has the same order.
    everything = _ids(await _page(client, admin_key))
    assert [run_id for run_id in everything if run_id in expected] == expected

    # A page exactly as long as the rest has no cursor; one shorter has.
    exact = await _page(client, admin_key, principalId=coder["id"], limit=3)
    assert _ids(exact) == expected
    assert exact["nextCursor"] is None
    first = await _page(client, admin_key, principalId=coder["id"], limit=2)
    assert _ids(first) == expected[:2]
    assert first["nextCursor"] is not None
    second = await _page(
        client, admin_key, principalId=coder["id"], limit=2, cursor=first["nextCursor"]
    )
    assert _ids(second) == expected[2:]
    assert second["nextCursor"] is None

    # Combined with the other filters.
    task_page = await _page(client, admin_key, principalId=coder["id"], taskId=runs[0]["taskId"])
    assert _ids(task_page) == [runs[0]["id"]]
    assert _ids(await _page(client, admin_key, principalId=coder["id"], status="running")) == []
    failed = await _page(client, admin_key, principalId=coder["id"], status="failed")
    assert _ids(failed) == expected


async def test_runs_started_at_the_same_instant_page_by_id(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    coder, coder_key = await create_agent_with_key(client, admin_key, name="coder")
    runs = [await _run(client, admin_key, coder_key) for _ in range(3)]
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET started_at = :at WHERE principal_id = :principal"),
            {"at": utcnow(), "principal": coder["id"]},
        )
    expected = sorted((run["id"] for run in runs), reverse=True)

    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"principalId": coder["id"], "limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = await _page(client, admin_key, **params)
        seen += _ids(page)
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert seen == expected


async def test_agent_key_resolves_to_the_principal_of_the_agent(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "engineering")
    await create_role(client, admin_key, "coder")
    revision = (await _publish(client, admin_key, coder_spec(workspace["id"]))).json()["revision"]
    principal_id = (await _link(client, admin_key)).json()["principalId"]
    agent_key = await _api_key(client, admin_key, principal_id)
    # Published, never bound to a principal: it has not run anything.
    await _publish(client, admin_key, coder_spec(workspace["id"]), agent="idle")
    runner, runner_key = await create_agent_with_key(client, admin_key, name="runner")

    task = {"workspaceId": workspace["id"]}
    agent_runs = [
        await _run(client, admin_key, agent_key, task=task, agentRevisionId=revision["id"])
        for _ in range(2)
    ]
    runner_run = await _run(client, admin_key, runner_key, task=task)

    page = await _page(client, admin_key, agentKey="coder")
    assert _ids(page) == [agent_runs[1]["id"], agent_runs[0]["id"]]
    assert runner_run["id"] not in _ids(page)
    assert _ids(await _page(client, admin_key, principalId=principal_id)) == _ids(page)

    # Unknown key and an agent without a principal: an empty page, not an error.
    assert await _page(client, admin_key, agentKey="ghost") == {"items": [], "nextCursor": None}
    assert await _page(client, admin_key, agentKey="idle") == {"items": [], "nextCursor": None}

    # Both filters name one executor or none.
    both = await _page(client, admin_key, agentKey="coder", principalId=principal_id, limit=1)
    assert _ids(both) == [agent_runs[1]["id"]]
    assert both["nextCursor"] is not None
    assert _ids(await _page(client, admin_key, agentKey="coder", principalId=runner["id"])) == []

    # The key is resolved within the caller's tenant.
    _, other_admin = make_tenant_directly(sync_engine, "globex")
    assert await _page(client, other_admin, agentKey="coder") == {"items": [], "nextCursor": None}

    # A retired agent keeps its history.
    retired = await client.post(
        "/api/v1/agents/coder:retire", json={"reason": "replaced"}, headers=auth(admin_key)
    )
    assert retired.status_code == 200, retired.text
    assert _ids(await _page(client, admin_key, agentKey="coder")) == _ids(page)


async def test_runs_of_another_tenant_are_not_listed(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    coder, coder_key = await create_agent_with_key(client, admin_key, name="coder")
    run = await _run(client, admin_key, coder_key)

    _, other_admin = make_tenant_directly(sync_engine, "globex")
    empty = {"items": [], "nextCursor": None}
    assert await _page(client, other_admin, principalId=coder["id"]) == empty
    assert await _page(client, other_admin, taskId=run["taskId"]) == empty
    assert _ids(await _page(client, admin_key, principalId=coder["id"])) == [run["id"]]


async def test_listing_runs_by_executor_needs_tasks_read(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    coder, _ = await create_agent_with_key(client, admin_key, name="coder")
    _, reader_key = await create_agent_with_key(
        client, admin_key, name="events-only", permissions=["events.read"]
    )
    for params in ({"principalId": coder["id"]}, {"agentKey": "coder"}):
        response = await client.get("/api/v1/runs", params=params, headers=auth(reader_key))
        assert response.status_code == 403, response.text
    invalid = await client.get(
        "/api/v1/runs", params={"principalId": "not-a-uuid"}, headers=auth(admin_key)
    )
    assert invalid.status_code == 400, invalid.text
    assert invalid.json()["error"]["code"] == "invalid_request"
