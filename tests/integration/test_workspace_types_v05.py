"""Workspace Types: the tenant's node-type registry and what it constrains.

The guarantee under test (ADR-0029): a type is a declarative constraint on the
single workspace tree, never an extension point. Its ``key`` is unique per
tenant, its ``fieldSchema`` is the only validator of a workspace's
``customFields``, and its ``allowedChildTypes`` is enforced on every structural
mutation — create, retype and move — in both directions. The per-tenant system
type ``generic`` always exists, always accepts any child, and can be neither
archived nor narrowed; a type still carrying an active workspace cannot be
archived, so the tree can never describe itself with a type nobody may select.
"""

from typing import Any

import httpx
from sqlalchemy.engine import Engine

from tests.helpers import (
    auth,
    create_agent_with_key,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
)


async def create_type(
    client: httpx.AsyncClient, key: str, type_key: str, **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/workspace-types",
        json={"key": type_key, "displayName": type_key.title(), **extra},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def get_system_type(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    response = await client.get("/api/v1/workspace-types", headers=auth(key))
    assert response.status_code == 200, response.text
    return next(t for t in response.json()["items"] if t["isSystem"])


async def test_workspace_type_crud_with_etag(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]

    created = await create_type(
        client,
        admin_key,
        "portfolio",
        fieldSchema={"type": "object", "properties": {"owner": {"type": "string"}}},
        allowedChildTypes=["team"],
    )
    assert created["key"] == "portfolio"
    assert created["displayName"] == "Portfolio"
    assert created["allowedChildTypes"] == ["team"]
    assert created["isSystem"] is False
    assert created["status"] == "active"
    assert created["version"] == 1

    response = await client.get(f"/api/v1/workspace-types/{created['id']}", headers=auth(admin_key))
    assert response.status_code == 200
    assert response.headers["etag"] == '"workspace_type-1"'
    assert response.json()["fieldSchema"]["properties"] == {"owner": {"type": "string"}}

    response = await client.patch(
        f"/api/v1/workspace-types/{created['id']}",
        json={"displayName": "Portfolio v2", "allowedChildTypes": ["team", "squad"]},
        headers={**auth(admin_key), "If-Match": '"workspace_type-1"'},
    )
    assert response.status_code == 200, response.text
    assert response.json()["displayName"] == "Portfolio v2"
    assert response.json()["allowedChildTypes"] == ["team", "squad"]
    assert response.json()["version"] == 2

    # Stale If-Match -> conflict; the write is refused, not merged.
    response = await client.patch(
        f"/api/v1/workspace-types/{created['id']}",
        json={"displayName": "Nope"},
        headers={**auth(admin_key), "If-Match": '"workspace_type-1"'},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "version_conflict"

    # Missing If-Match -> precondition required.
    response = await client.patch(
        f"/api/v1/workspace-types/{created['id']}",
        json={"displayName": "Nope"},
        headers=auth(admin_key),
    )
    assert response.status_code == 428
    assert response.json()["error"]["code"] == "if_match_required"

    keys = [
        t["key"]
        for t in (await client.get("/api/v1/workspace-types", headers=auth(admin_key))).json()[
            "items"
        ]
    ]
    assert sorted(keys) == ["generic", "portfolio"]


async def test_duplicate_type_key_is_conflict(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_type(client, admin_key, "team")

    response = await client.post(
        "/api/v1/workspace-types",
        json={"key": "team", "displayName": "Team Again"},
        headers=auth(admin_key),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "workspace_type_exists"
    assert response.json()["error"]["details"]["key"] == "team"

    # The system key is reserved by the same rule.
    response = await client.post(
        "/api/v1/workspace-types",
        json={"key": "generic", "displayName": "Mine"},
        headers=auth(admin_key),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "workspace_type_exists"


async def test_system_type_exists_and_is_immutable(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]

    system_type = await get_system_type(client, admin_key)
    assert system_type["key"] == "generic"
    assert system_type["isSystem"] is True
    assert system_type["allowedChildTypes"] == ["*"]
    assert system_type["status"] == "active"

    response = await client.post(
        f"/api/v1/workspace-types/{system_type['id']}:archive", headers=auth(admin_key)
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "system_type_immutable"

    # The wildcard may not be taken away either.
    response = await client.patch(
        f"/api/v1/workspace-types/{system_type['id']}",
        json={"allowedChildTypes": ["team"]},
        headers={**auth(admin_key), "If-Match": f'"workspace_type-{system_type["version"]}"'},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "system_type_immutable"

    # Still intact after the refused writes.
    assert (await get_system_type(client, admin_key))["allowedChildTypes"] == ["*"]


async def test_parent_child_rules_on_create(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_type(client, admin_key, "team", allowedChildTypes=[])
    await create_type(client, admin_key, "portfolio", allowedChildTypes=["team"])

    portfolio = await create_workspace(client, admin_key, "alpha", typeKey="portfolio")

    # A portfolio inside a portfolio is not an allowed child.
    response = await client.post(
        "/api/v1/workspaces",
        json={
            "slug": "beta",
            "name": "Beta",
            "parentId": portfolio["id"],
            "typeKey": "portfolio",
        },
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "child_type_not_allowed"
    details = response.json()["error"]["details"]
    assert details["parentTypeKey"] == "portfolio"
    assert details["childTypeKey"] == "portfolio"
    assert details["allowedChildTypes"] == ["team"]

    # An allowed child passes.
    team = await create_workspace(
        client, admin_key, "core", parent_id=portfolio["id"], typeKey="team"
    )
    assert team["parentId"] == portfolio["id"]

    # A leaf type refuses every child.
    response = await client.post(
        "/api/v1/workspaces",
        json={"slug": "sub", "name": "Sub", "parentId": team["id"], "typeKey": "team"},
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "child_type_not_allowed"

    # A ROOT workspace may use any type: there is no parent rule to satisfy.
    root_team = await create_workspace(client, admin_key, "standalone", typeKey="team")
    assert root_team["parentId"] is None


async def test_unknown_and_ambiguous_type_reference(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    team = await create_type(client, admin_key, "team")

    response = await client.post(
        "/api/v1/workspaces",
        json={"slug": "ghost", "name": "Ghost", "typeKey": "nosuchtype"},
        headers=auth(admin_key),
    )
    assert response.status_code == 404
    assert response.json()["error"]["details"]["typeKey"] == "nosuchtype"

    response = await client.post(
        "/api/v1/workspaces",
        json={"slug": "both", "name": "Both", "typeId": team["id"], "typeKey": "team"},
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_workspace_type"

    # Omitting the type entirely falls back to the tenant's system type.
    workspace = await create_workspace(client, admin_key, "plain")
    system_type = await get_system_type(client, admin_key)
    assert workspace["typeId"] == system_type["id"]


async def test_retype_revalidates_both_directions(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_type(client, admin_key, "team", allowedChildTypes=[])
    await create_type(client, admin_key, "squad", allowedChildTypes=[])
    await create_type(client, admin_key, "portfolio", allowedChildTypes=["team", "squad"])

    portfolio = await create_workspace(client, admin_key, "alpha", typeKey="portfolio")
    team = await create_workspace(
        client, admin_key, "core", parent_id=portfolio["id"], typeKey="team"
    )

    # Parent rule: the child's new type must be allowed under its parent.
    response = await client.patch(
        f"/api/v1/workspaces/{team['id']}",
        json={"typeKey": "portfolio"},
        headers={**auth(admin_key), "If-Match": '"workspace-1"'},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "child_type_not_allowed"
    assert response.json()["error"]["details"]["parentTypeKey"] == "portfolio"

    # Existing-children rule: the parent's new type must still accept its
    # children. ``generic`` accepts anything, ``team`` accepts nothing.
    generic_parent = await create_workspace(client, admin_key, "holding")
    await create_workspace(client, admin_key, "unit", parent_id=generic_parent["id"])

    response = await client.patch(
        f"/api/v1/workspaces/{generic_parent['id']}",
        json={"typeKey": "team"},
        headers={**auth(admin_key), "If-Match": '"workspace-1"'},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "child_type_not_allowed"
    assert response.json()["error"]["details"]["parentTypeKey"] == "team"

    # A retype legal in both directions succeeds and bumps the version: the
    # workspace has no children and "squad" is allowed under "portfolio".
    response = await client.patch(
        f"/api/v1/workspaces/{team['id']}",
        json={"typeKey": "squad"},
        headers={**auth(admin_key), "If-Match": '"workspace-1"'},
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2


async def test_move_under_forbidding_parent_rejected(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_type(client, admin_key, "team")
    await create_type(client, admin_key, "portfolio", allowedChildTypes=["team"])

    portfolio = await create_workspace(client, admin_key, "alpha", typeKey="portfolio")
    other = await create_workspace(client, admin_key, "beta", typeKey="portfolio")

    response = await client.post(
        f"/api/v1/workspaces/{other['id']}:move",
        json={"newParentId": portfolio["id"]},
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "child_type_not_allowed"

    # The refused move left the workspace where it was.
    response = await client.get(f"/api/v1/workspaces/{other['id']}", headers=auth(admin_key))
    assert response.json()["parentId"] is None
    assert response.json()["version"] == 1

    # A workspace whose type IS allowed moves in.
    team = await create_workspace(client, admin_key, "core", typeKey="team")
    response = await client.post(
        f"/api/v1/workspaces/{team['id']}:move",
        json={"newParentId": portfolio["id"]},
        headers=auth(admin_key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["parentId"] == portfolio["id"]


async def test_field_schema_validates_custom_fields(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_type(
        client,
        admin_key,
        "team",
        fieldSchema={
            "type": "object",
            "properties": {"headcount": {"type": "integer"}},
            "required": ["headcount"],
        },
    )

    workspace = await create_workspace(
        client, admin_key, "core", typeKey="team", customFields={"headcount": 7}
    )
    assert workspace["customFields"] == {"headcount": 7}

    response = await client.post(
        "/api/v1/workspaces",
        json={
            "slug": "broken",
            "name": "Broken",
            "typeKey": "team",
            "customFields": {"headcount": "many"},
        },
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "custom_fields_invalid"
    assert error["details"]["field"] == "customFields"
    assert [e["path"] for e in error["details"]["errors"]] == ["/headcount"]

    # The same schema guards updates.
    response = await client.patch(
        f"/api/v1/workspaces/{workspace['id']}",
        json={"customFields": {}},
        headers={**auth(admin_key), "If-Match": '"workspace-1"'},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "custom_fields_invalid"
    assert response.json()["error"]["details"]["errors"][0]["path"] == "/"


async def test_invalid_json_schema_rejected(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]

    response = await client.post(
        "/api/v1/workspace-types",
        json={
            "key": "broken",
            "displayName": "Broken",
            "fieldSchema": {"type": "not-a-json-type"},
        },
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_json_schema"
    assert error["details"]["field"] == "fieldSchema"

    # Nothing was written: the key is still free.
    await create_type(client, admin_key, "broken")

    # The same guard applies on update.
    existing = await create_type(client, admin_key, "team")
    response = await client.patch(
        f"/api/v1/workspace-types/{existing['id']}",
        json={"fieldSchema": {"required": "headcount"}},
        headers={**auth(admin_key), "If-Match": '"workspace_type-1"'},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_json_schema"


async def test_archive_type_in_use_and_reuse_blocked(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    used = await create_type(client, admin_key, "team")
    unused = await create_type(client, admin_key, "squad")

    workspace = await create_workspace(client, admin_key, "core", typeKey="team")

    response = await client.post(
        f"/api/v1/workspace-types/{used['id']}:archive", headers=auth(admin_key)
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "workspace_type_in_use"
    assert error["details"]["activeWorkspaces"] == 1

    # An unused type archives, is idempotent, and can no longer be selected.
    response = await client.post(
        f"/api/v1/workspace-types/{unused['id']}:archive", headers=auth(admin_key)
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "archived"
    assert (
        await client.post(
            f"/api/v1/workspace-types/{unused['id']}:archive", headers=auth(admin_key)
        )
    ).status_code == 200

    response = await client.post(
        "/api/v1/workspaces",
        json={"slug": "late", "name": "Late", "typeKey": "squad"},
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "workspace_type_archived"

    # Archiving the last active workspace frees its type.
    assert (
        await client.post(f"/api/v1/workspaces/{workspace['id']}:archive", headers=auth(admin_key))
    ).status_code == 200
    response = await client.post(
        f"/api/v1/workspace-types/{used['id']}:archive", headers=auth(admin_key)
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "archived"

    archived = [
        t["key"]
        for t in (
            await client.get("/api/v1/workspace-types?status=archived", headers=auth(admin_key))
        ).json()["items"]
    ]
    assert sorted(archived) == ["squad", "team"]


async def test_workspace_type_authorization(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    workspace_type = await create_type(client, admin_key, "team")

    _, reader_key = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["workspaces.read"]
    )
    _, unrelated_key = await create_agent_with_key(
        client, admin_key, name="unrelated", permissions=["projects.read"]
    )

    assert (
        await client.get("/api/v1/workspace-types", headers=auth(reader_key))
    ).status_code == 200
    assert (
        await client.get(
            f"/api/v1/workspace-types/{workspace_type['id']}", headers=auth(reader_key)
        )
    ).status_code == 200

    # Reading does not imply reshaping the registry.
    response = await client.post(
        "/api/v1/workspace-types",
        json={"key": "squad", "displayName": "Squad"},
        headers=auth(reader_key),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"

    response = await client.post(
        f"/api/v1/workspace-types/{workspace_type['id']}:archive", headers=auth(reader_key)
    )
    assert response.status_code == 403

    assert (
        await client.get("/api/v1/workspace-types", headers=auth(unrelated_key))
    ).status_code == 403


async def test_workspace_type_tenant_isolation(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    team = await create_type(client, admin_key, "team")

    _, key_b = make_tenant_directly(sync_engine, "tenant-b")

    # Tenant B sees only its own registry.
    keys = [
        t["key"]
        for t in (await client.get("/api/v1/workspace-types", headers=auth(key_b))).json()["items"]
    ]
    assert keys == ["generic"]

    assert (
        await client.get(f"/api/v1/workspace-types/{team['id']}", headers=auth(key_b))
    ).status_code == 404
    assert (
        await client.post(f"/api/v1/workspace-types/{team['id']}:archive", headers=auth(key_b))
    ).status_code == 404

    # Nor can it build a workspace on the other tenant's type, by id or by key.
    response = await client.post(
        "/api/v1/workspaces",
        json={"slug": "steal", "name": "Steal", "typeId": team["id"]},
        headers=auth(key_b),
    )
    assert response.status_code == 404
    response = await client.post(
        "/api/v1/workspaces",
        json={"slug": "steal", "name": "Steal", "typeKey": "team"},
        headers=auth(key_b),
    )
    assert response.status_code == 404

    # The key is free on the other side of the boundary.
    await create_type(client, key_b, "team")
