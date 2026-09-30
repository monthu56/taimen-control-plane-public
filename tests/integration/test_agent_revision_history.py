"""The revision history of an agent in one request (CP-ADR-0073, amendment 2026-09-29, Г).

``GET /agents/{key}/revisions`` lists the revisions newest first: number,
hash, who published and from which source, whether it is active and which
spec fields changed against the revision before it. The spec itself stays
behind ``key@revision``.
"""

import copy
from pathlib import Path
from typing import Any

import httpx
import yaml
from sqlalchemy.engine import Engine

from tests.helpers import auth, do_bootstrap, make_tenant_directly

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agents"
BRIDGE: dict[str, Any] = yaml.safe_load(
    (FIXTURES / "process-bridge.yaml").read_text(encoding="utf-8")
)["spec"]


def _spec(**changes: Any) -> dict[str, Any]:
    spec = copy.deepcopy(BRIDGE)
    spec.update(changes)
    return spec


async def _publish(
    client: httpx.AsyncClient,
    key: str,
    spec: dict[str, Any],
    *,
    agent: str = "bridge",
    package: dict[str, str] | None = None,
    expected: int = 201,
) -> dict[str, Any]:
    body: dict[str, Any] = {"key": agent, "spec": spec}
    if package is not None:
        body["package"] = package
    response = await client.post("/api/v1/agents", json=body, headers=auth(key))
    assert response.status_code == expected, response.text
    published: dict[str, Any] = response.json()
    return published


async def _history(
    client: httpx.AsyncClient, key: str, agent: str = "bridge", **params: Any
) -> dict[str, Any]:
    response = await client.get(
        f"/api/v1/agents/{agent}/revisions", params=params, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    page: dict[str, Any] = response.json()
    return page


async def test_a_single_revision(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    published = await _publish(client, admin_key, _spec())

    page = await _history(client, admin_key)
    assert page["nextCursor"] is None
    [item] = page["items"]
    revision = published["revision"]
    assert item == {
        "id": revision["id"],
        "agentKey": "bridge",
        "revision": 1,
        "specHash": revision["specHash"],
        "createdBy": revision["createdBy"],
        "createdAt": revision["createdAt"],
        "source": {"kind": "manual", "package": None},
        "active": True,
        "changedFields": None,
    }


async def test_several_revisions_newest_first_with_their_changes(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    first = await _publish(client, admin_key, _spec())
    package = {"key": "selfdev", "version": "1.4.0"}
    identity = {**BRIDGE["identity"], "permissions": ["tasks.read", "events.read"]}
    await _publish(
        client, admin_key, _spec(description="The bridge", identity=identity), package=package
    )
    # The same spec again and a desired-state change write no revision.
    await _publish(
        client, admin_key, _spec(description="The bridge", identity=identity), expected=200
    )
    stopped = await client.patch(
        "/api/v1/agents/bridge/state", json={"state": "stopped"}, headers=auth(admin_key)
    )
    assert stopped.status_code == 200, stopped.text
    # A rollback is a new revision with the hash of the first one (§2).
    await _publish(client, admin_key, _spec())

    items = (await _history(client, admin_key))["items"]
    assert [i["revision"] for i in items] == [3, 2, 1]
    assert [i["active"] for i in items] == [True, False, False]
    assert items[0]["specHash"] == first["revision"]["specHash"]
    assert [i["source"] for i in items] == [
        {"kind": "manual", "package": None},
        {"kind": "package", "package": package},
        {"kind": "manual", "package": None},
    ]
    assert [i["changedFields"] for i in items] == [
        ["description", "identity.permissions"],
        ["description", "identity.permissions"],
        None,
    ]
    assert all("spec" not in i for i in items)

    # Each item is the revision key@N addresses.
    for item in items:
        pinned = await client.get(
            f"/api/v1/agents/bridge@{item['revision']}", headers=auth(admin_key)
        )
        assert pinned.status_code == 200, pinned.text
        assert pinned.json()["revision"]["id"] == item["id"]
        assert pinned.json()["revision"]["specHash"] == item["specHash"]


async def test_a_package_applying_the_same_spec_adds_no_revision(
    client: httpx.AsyncClient,
) -> None:
    """The source belongs to a new revision; an unchanged spec keeps the old one's."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await _publish(client, admin_key, _spec())
    await _publish(
        client, admin_key, _spec(), package={"key": "selfdev", "version": "2"}, expected=200
    )

    [item] = (await _history(client, admin_key))["items"]
    assert item["source"] == {"kind": "manual", "package": None}


async def test_a_retired_agent_keeps_its_history_without_an_active_revision(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await _publish(client, admin_key, _spec())
    await _publish(client, admin_key, _spec(description="v2"))
    retired = await client.post(
        "/api/v1/agents/bridge:retire", json={"reason": "replaced"}, headers=auth(admin_key)
    )
    assert retired.status_code == 200, retired.text

    items = (await _history(client, admin_key))["items"]
    assert [i["revision"] for i in items] == [2, 1]
    assert [i["active"] for i in items] == [False, False]
    assert items[0]["changedFields"] == ["description"]


async def test_the_cursor_walks_every_revision_once(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    for number in range(1, 6):
        await _publish(client, admin_key, _spec(description=f"v{number}"))

    seen: list[list[int]] = []
    changes: list[list[str] | None] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        page = await _history(client, admin_key, **params)
        seen.append([i["revision"] for i in page["items"]])
        changes.extend(i["changedFields"] for i in page["items"])
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert seen == [[5, 4], [3, 2], [1]]
    # The last item of a page still knows what it changed: the one before it
    # is on the next page.
    assert changes == [["description"]] * 4 + [None]

    # A page that ends exactly at revision 1 has no next page.
    exact = await _history(client, admin_key, limit=5)
    assert ([i["revision"] for i in exact["items"]], exact["nextCursor"]) == (
        [5, 4, 3, 2, 1],
        None,
    )


async def test_a_bad_cursor_or_limit_is_refused(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await _publish(client, admin_key, _spec())

    for params, code in (
        ({"cursor": "not-a-cursor"}, "invalid_cursor"),
        ({"cursor": "eyJyIjoieCJ9"}, "invalid_cursor"),  # {"r":"x"}
        ({"limit": 0}, "invalid_limit"),
    ):
        response = await client.get(
            "/api/v1/agents/bridge/revisions", params=params, headers=auth(admin_key)
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == code


async def test_an_unknown_agent_is_not_found(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await client.get("/api/v1/agents/ghost/revisions", headers=auth(admin_key))
    assert response.status_code == 404, response.text


async def test_another_tenant_sees_only_its_own_agent(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    await _publish(client, admin_key, _spec())
    await _publish(client, admin_key, _spec(description="v2"))
    _, other_key = make_tenant_directly(sync_engine, "other")

    hidden = await client.get("/api/v1/agents/bridge/revisions", headers=auth(other_key))
    assert hidden.status_code == 404, hidden.text

    # The same key in the other tenant is another agent with its own history.
    await _publish(client, other_key, _spec())
    items = (await _history(client, other_key))["items"]
    assert [i["revision"] for i in items] == [1]
    assert len((await _history(client, admin_key))["items"]) == 2
