"""v0.5 races: one project per workspace, one winning revision, safe moves.

Each of these is a place where a check-then-write would be wrong under
concurrency. The invariants are enforced by the database (a unique index, a
row lock inside the command transaction), so the tests assert the *outcome*
distribution, not the implementation.
"""

import asyncio

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import auth, create_workspace, do_bootstrap


async def _template(client: httpx.AsyncClient, admin_key: str, key: str = "delivery") -> dict:
    response = await client.post(
        "/api/v1/project-templates",
        json={"key": key, "displayName": key.title()},
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_twenty_concurrent_project_creates_yield_exactly_one(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """UNIQUE(workspace_id) is the arbiter: 1 profile, 1 event, 19 conflicts."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "contended")
    template = await _template(client, admin_key)

    async def create() -> httpx.Response:
        return await client.post(
            "/api/v1/projects",
            json={"workspaceId": workspace["id"], "templateId": template["id"]},
            headers=auth(admin_key),
        )

    responses = await asyncio.gather(*[create() for _ in range(20)])
    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(201) == 1, statuses
    assert statuses.count(409) == 19, statuses
    for response in responses:
        if response.status_code == 409:
            error = response.json()["error"]
            assert error["code"] == "project_exists"
            assert error["details"]["workspaceId"] == workspace["id"]

    with sync_engine.connect() as conn:
        profiles = conn.execute(
            text("SELECT count(*) FROM project_profiles WHERE workspace_id = :ws"),
            {"ws": workspace["id"]},
        ).scalar()
        created_events = conn.execute(
            text("SELECT count(*) FROM events WHERE event_type = 'project.created'")
        ).scalar()
    assert profiles == 1
    assert created_events == 1, "a rolled-back create must not leave an event behind"


async def test_concurrent_activation_of_two_revisions_has_one_winner(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Both callers present the same expected version; exactly one may win."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    template = await _template(client, admin_key)
    project = (
        await client.post(
            "/api/v1/projects",
            json={"workspaceSlug": "rev-race", "templateId": template["id"]},
            headers=auth(admin_key),
        )
    ).json()

    for value in ("one", "two"):
        response = await client.post(
            f"/api/v1/projects/{project['id']}/config-revisions",
            json={"config": {"settings": {"pick": value}}},
            headers=auth(admin_key),
        )
        assert response.status_code == 201, response.text

    version = project["version"]

    async def activate(revision: int) -> httpx.Response:
        return await client.post(
            f"/api/v1/projects/{project['id']}/config-revisions/{revision}:activate",
            headers={**auth(admin_key), "If-Match": f'"project-{version}"'},
        )

    responses = await asyncio.gather(activate(1), activate(2))
    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(200) == 1, [r.text for r in responses]
    assert statuses.count(409) == 1
    loser = next(r for r in responses if r.status_code == 409)
    assert loser.json()["error"]["code"] == "version_conflict"

    winner = next(r for r in responses if r.status_code == 200).json()
    current = (
        await client.get(f"/api/v1/projects/{project['id']}", headers=auth(admin_key))
    ).json()
    assert current["activeConfigRevision"] == winner["activeConfigRevision"]
    assert current["version"] == version + 1

    with sync_engine.connect() as conn:
        activated = conn.execute(
            text(
                "SELECT count(*) FROM events WHERE event_type = 'project.config_revision_activated'"
            )
        ).scalar()
        pointers = conn.execute(
            text(
                "SELECT count(*) FROM project_config_revisions"
                " WHERE project_id = :p AND activated_at IS NOT NULL"
            ),
            {"p": project["id"]},
        ).scalar()
    assert activated == 1
    assert pointers == 1, "only the winning revision may be marked activated"


async def test_move_and_activation_cannot_produce_a_weakened_hierarchy(
    client: httpx.AsyncClient,
) -> None:
    """A racing move and revision activation still end inside the ceiling.

    ``strict`` allows at most 10 run actions; ``lax`` has no ceiling. The child
    starts under ``lax`` and tries to move under ``strict`` while activating a
    revision that would only be legal under ``lax``. Whatever the interleaving,
    the committed state must satisfy the ancestor governance.
    """
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    template = await _template(client, admin_key)

    strict = (
        await client.post(
            "/api/v1/projects",
            json={
                "workspaceSlug": "strict",
                "templateId": template["id"],
                "settings": {},
            },
            headers=auth(admin_key),
        )
    ).json()
    strict_revision = await client.post(
        f"/api/v1/projects/{strict['id']}/config-revisions",
        json={"config": {"governance": {"maxRunActions": 10}}},
        headers=auth(admin_key),
    )
    assert strict_revision.status_code == 201
    activated = await client.post(
        f"/api/v1/projects/{strict['id']}/config-revisions/1:activate",
        headers={**auth(admin_key), "If-Match": f'"project-{strict["version"]}"'},
    )
    assert activated.status_code == 200, activated.text

    lax = (
        await client.post(
            "/api/v1/projects",
            json={"workspaceSlug": "lax", "templateId": template["id"]},
            headers=auth(admin_key),
        )
    ).json()
    child = (
        await client.post(
            "/api/v1/projects",
            json={
                "workspaceSlug": "child",
                "parentWorkspaceId": lax["workspaceId"],
                "templateId": template["id"],
            },
            headers=auth(admin_key),
        )
    ).json()
    lax_only = await client.post(
        f"/api/v1/projects/{child['id']}/config-revisions",
        json={"config": {"governance": {"maxRunActions": 500}}},
        headers=auth(admin_key),
    )
    assert lax_only.status_code == 201, lax_only.text

    strict_workspace = (
        await client.get(f"/api/v1/projects/{strict['id']}", headers=auth(admin_key))
    ).json()["workspaceId"]

    async def move() -> httpx.Response:
        return await client.post(
            f"/api/v1/workspaces/{child['workspaceId']}:move",
            json={"newParentId": strict_workspace},
            headers=auth(admin_key),
        )

    async def activate() -> httpx.Response:
        return await client.post(
            f"/api/v1/projects/{child['id']}/config-revisions/1:activate",
            headers={**auth(admin_key), "If-Match": f'"project-{child["version"]}"'},
        )

    move_response, activate_response = await asyncio.gather(move(), activate())

    # Whatever happened, the committed hierarchy must be consistent.
    workspace = (
        await client.get(f"/api/v1/workspaces/{child['workspaceId']}", headers=auth(admin_key))
    ).json()
    effective = (
        await client.get(
            f"/api/v1/projects/{child['id']}/effective-config", headers=auth(admin_key)
        )
    ).json()
    moved = workspace["parentId"] == strict_workspace
    activated_lax = activate_response.status_code == 200

    assert not (
        moved and activated_lax and effective["config"]["governance"].get("maxRunActions") == 500
    ), "a move plus a lax activation must never both stand"
    if moved:
        # Under the strict parent the effective ceiling is the parent's.
        assert effective["config"]["governance"]["maxRunActions"] <= 10
    else:
        assert move_response.status_code in (200, 422)


async def test_concurrent_workspace_type_creation_is_unique(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]

    async def create() -> httpx.Response:
        return await client.post(
            "/api/v1/workspace-types",
            json={"key": "team", "displayName": "Team"},
            headers=auth(admin_key),
        )

    responses = await asyncio.gather(*[create() for _ in range(8)])
    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(201) == 1, statuses
    assert set(statuses[1:]) == {409}
    with sync_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM workspace_types WHERE key = 'team'")
        ).scalar()
    assert count == 1


async def test_concurrent_template_versions_are_monotonic(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The per-key advisory lock hands out 1..N with no gaps and no duplicates."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]

    async def create(index: int) -> httpx.Response:
        return await client.post(
            "/api/v1/project-templates",
            json={"key": "delivery", "displayName": f"Delivery {index}"},
            headers=auth(admin_key),
        )

    responses = await asyncio.gather(*[create(i) for i in range(6)])
    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    versions = sorted(r.json()["version"] for r in responses)
    assert versions == list(range(1, 7))
