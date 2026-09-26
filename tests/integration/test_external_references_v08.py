"""Generic external references over work items (ADR-0047, TASK-000026).

Guarantee under test: an external identifier maps onto *any* registered entity
type under the permission that already governs that entity; registration is
idempotent by external key; the mapping never crosses a tenant boundary; and
the project scope keeps its contract because it is the same code path.
"""

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import (
    auth,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
)

SYM = {"externalSystem": "external-root", "externalType": "task", "externalId": "SYM-T-070"}


async def _create_project(client: httpx.AsyncClient, admin_key: str, slug: str) -> dict[str, Any]:
    template = await client.post(
        "/api/v1/project-templates",
        json={"key": f"tpl-{slug}", "displayName": slug.title()},
        headers=auth(admin_key),
    )
    assert template.status_code == 201, template.text
    workspace = await create_workspace(client, admin_key, slug)
    response = await client.post(
        "/api/v1/projects",
        json={"workspaceId": workspace["id"], "templateId": template.json()["id"]},
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _register(
    client: httpx.AsyncClient, key: str, *, entity_type: str, entity_id: str, **body: Any
) -> httpx.Response:
    return await client.post(
        "/api/v1/external-references",
        json={"entityType": entity_type, "entityId": entity_id, **body},
        headers=auth(key),
    )


def _count_events(sync_engine: Engine, event_type: str) -> int:
    with sync_engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM events WHERE event_type = :t"), {"t": event_type}
            ).scalar_one()
        )


# --- registration and idempotency ---------------------------------------------


async def test_task_reference_registers_and_repeats_idempotently(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The legacy identifier of a migrated task becomes a row, not a text line.

    Re-running an import must not create a second mapping and must not bump the
    version — that is what makes the run repeatable (ADR-0014).
    """
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Migrated task")

    created = await _register(client, admin_key, entity_type="task", entity_id=task["id"], **SYM)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["entityType"] == "task"
    assert body["entityId"] == task["id"]
    assert body["version"] == 1
    assert _count_events(sync_engine, "task.external_reference_added") == 1

    # Same mapping, same metadata: nothing to write.
    repeat = await _register(client, admin_key, entity_type="task", entity_id=task["id"], **SYM)
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["id"] == body["id"]
    assert repeat.json()["version"] == 1
    assert _count_events(sync_engine, "task.external_reference_added") == 1
    assert _count_events(sync_engine, "task.external_reference_updated") == 0

    # Same mapping, new metadata: metadata only, version moves, event recorded.
    updated = await _register(
        client,
        admin_key,
        entity_type="task",
        entity_id=task["id"],
        **SYM,
        metadata={"sourcePath": "operations/tasks/SYM-T-070.md"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["id"] == body["id"]
    assert updated.json()["version"] == 2
    assert updated.json()["metadata"] == {"sourcePath": "operations/tasks/SYM-T-070.md"}
    assert _count_events(sync_engine, "task.external_reference_updated") == 1

    with sync_engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM external_references")).scalar_one()
    assert rows == 1


async def test_task_can_be_addressed_by_public_id(client: httpx.AsyncClient) -> None:
    """An importer holds public ids, so the generic API must accept them."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Migrated task")

    created = await _register(
        client, admin_key, entity_type="task", entity_id=task["publicId"], **SYM
    )
    assert created.status_code == 201, created.text
    assert created.json()["entityId"] == task["id"]

    listed = await client.get(
        "/api/v1/external-references",
        params={"entityType": "task", "entityId": task["publicId"]},
        headers=auth(admin_key),
    )
    assert listed.status_code == 200, listed.text
    assert [r["id"] for r in listed.json()["items"]] == [created.json()["id"]]


async def test_external_key_is_exclusive_across_entity_types(client: httpx.AsyncClient) -> None:
    """One external key maps to exactly one entity — across types, not per type."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Migrated task")
    project = await _create_project(client, admin_key, "acme")

    assert (
        await _register(client, admin_key, entity_type="task", entity_id=task["id"], **SYM)
    ).status_code == 201

    other_task = await create_task(client, admin_key, title="Another task")
    conflict = await _register(
        client, admin_key, entity_type="task", entity_id=other_task["id"], **SYM
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "external_reference_conflict"
    assert conflict.json()["error"]["details"]["entityId"] == task["id"]

    cross_type = await _register(
        client, admin_key, entity_type="project", entity_id=project["id"], **SYM
    )
    assert cross_type.status_code == 409, cross_type.text
    assert cross_type.json()["error"]["details"]["entityType"] == "task"


async def test_unknown_entity_type_is_rejected_before_any_write(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """An unresolvable type must not be able to occupy an external key."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]

    response = await _register(
        client, admin_key, entity_type="incident", entity_id="whatever", **SYM
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_entity_type"
    assert error["details"]["supported"] == ["project", "task"]

    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM external_references")).scalar_one() == 0


async def test_reference_to_a_missing_entity_is_rejected(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    missing = await _register(
        client,
        admin_key,
        entity_type="task",
        entity_id="00000000-0000-0000-0000-000000000000",
        **SYM,
    )
    assert missing.status_code == 404, missing.text

    malformed = await _register(client, admin_key, entity_type="task", entity_id="not-an-id", **SYM)
    assert malformed.status_code == 404, malformed.text


async def test_secret_material_in_metadata_is_refused(client: httpx.AsyncClient) -> None:
    """The metadata guard is a property of the command, not of the project path."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Migrated task")

    response = await _register(
        client,
        admin_key,
        entity_type="task",
        entity_id=task["id"],
        **SYM,
        metadata={"token": "sk-live-0123456789abcdefghijklmnop"},
    )
    assert response.status_code == 422, response.text


# --- authorization ------------------------------------------------------------


async def test_permission_follows_the_entity_type_not_the_project(
    client: httpx.AsyncClient,
) -> None:
    """A task key maps tasks; a project key maps projects. Neither borrows the other."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Migrated task")
    project = await _create_project(client, admin_key, "acme")

    _, task_key = await create_agent_with_key(
        client, admin_key, name="importer", permissions=["tasks.read", "tasks.write"]
    )
    _, project_key = await create_agent_with_key(
        client, admin_key, name="curator", permissions=["projects.read", "projects.manage"]
    )

    allowed = await _register(client, task_key, entity_type="task", entity_id=task["id"], **SYM)
    assert allowed.status_code == 201, allowed.text

    denied = await _register(
        client,
        task_key,
        entity_type="project",
        entity_id=project["id"],
        **{**SYM, "externalId": "P-1"},
    )
    assert denied.status_code == 403, denied.text

    denied_task = await _register(
        client,
        project_key,
        entity_type="task",
        entity_id=task["id"],
        **{**SYM, "externalId": "T-2"},
    )
    assert denied_task.status_code == 403, denied_task.text

    allowed_project = await _register(
        client,
        project_key,
        entity_type="project",
        entity_id=project["id"],
        **{**SYM, "externalId": "P-1"},
    )
    assert allowed_project.status_code == 201, allowed_project.text


async def test_reverse_lookup_hides_types_the_caller_cannot_read(
    client: httpx.AsyncClient,
) -> None:
    """Absence, not 403: otherwise the endpoint is an oracle over external ids."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Migrated task")
    assert (
        await _register(client, admin_key, entity_type="task", entity_id=task["id"], **SYM)
    ).status_code == 201

    found = await client.get(
        "/api/v1/external-references",
        params={"externalSystem": SYM["externalSystem"], "externalId": SYM["externalId"]},
        headers=auth(admin_key),
    )
    assert found.status_code == 200, found.text
    assert [r["entityId"] for r in found.json()["items"]] == [task["id"]]

    _, project_only = await create_agent_with_key(
        client, admin_key, name="curator", permissions=["projects.read"]
    )
    hidden = await client.get(
        "/api/v1/external-references",
        params={"externalSystem": SYM["externalSystem"], "externalId": SYM["externalId"]},
        headers=auth(project_only),
    )
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["items"] == []

    # A key that cannot read the type also cannot enumerate it forwards.
    forward = await client.get(
        "/api/v1/external-references",
        params={"entityType": "task", "entityId": task["id"]},
        headers=auth(project_only),
    )
    assert forward.status_code == 403, forward.text


async def test_reference_does_not_cross_the_tenant_boundary(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Migrated task")
    assert (
        await _register(client, admin_key, entity_type="task", entity_id=task["id"], **SYM)
    ).status_code == 201

    _, other_key = make_tenant_directly(sync_engine, "other")

    # Our task id is not resolvable there, so it reads as missing, not forbidden.
    stranger = await _register(client, other_key, entity_type="task", entity_id=task["id"], **SYM)
    assert stranger.status_code == 404, stranger.text

    lookup = await client.get(
        "/api/v1/external-references",
        params={"externalSystem": SYM["externalSystem"], "externalId": SYM["externalId"]},
        headers=auth(other_key),
    )
    assert lookup.status_code == 200, lookup.text
    assert lookup.json()["items"] == []


# --- lookup arguments ---------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"entityType": "task"},
        {"externalId": "SYM-T-070"},
        {"externalSystem": "external-root"},
        {"entityType": "task", "entityId": "TASK-000001", "externalSystem": "external-root"},
    ],
)
async def test_ambiguous_lookup_is_refused(
    client: httpx.AsyncClient, params: dict[str, str]
) -> None:
    """A half-specified filter would silently mean "return everything"."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await client.get(
        "/api/v1/external-references", params=params, headers=auth(admin_key)
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_external_lookup"


# --- project scope stays a compatible special case ----------------------------


async def test_project_scope_keeps_its_contract(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Same paths, same codes, same event types — the project is one entity type."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    project = await _create_project(client, admin_key, "acme")
    triple = {"externalSystem": "jira", "externalType": "board", "externalId": "ACME-1"}

    created = await client.post(
        f"/api/v1/projects/{project['id']}/external-references",
        json=triple,
        headers=auth(admin_key),
    )
    assert created.status_code == 201, created.text
    assert created.json()["entityType"] == "project"
    assert _count_events(sync_engine, "project.external_reference_added") == 1

    listed = await client.get(
        f"/api/v1/projects/{project['id']}/external-references", headers=auth(admin_key)
    )
    assert listed.status_code == 200, listed.text
    assert [r["id"] for r in listed.json()["items"]] == [created.json()["id"]]

    # The same reference is visible through the generic endpoint.
    generic = await client.get(
        "/api/v1/external-references",
        params={"entityType": "project", "entityId": project["id"]},
        headers=auth(admin_key),
    )
    assert [r["id"] for r in generic.json()["items"]] == [created.json()["id"]]

    missing = await client.get(
        f"/api/v1/projects/{'0' * 8}-0000-0000-0000-000000000000/external-references",
        headers=auth(admin_key),
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["error"]["details"] == {
        "projectId": "00000000-0000-0000-0000-000000000000"
    }


async def test_task_mapping_does_not_leak_into_project_lookup(client: httpx.AsyncClient) -> None:
    """The project filter is scoped to entity_type='project' and stays that way."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Migrated task")
    await _create_project(client, admin_key, "acme")
    assert (
        await _register(client, admin_key, entity_type="task", entity_id=task["id"], **SYM)
    ).status_code == 201

    projects = await client.get(
        "/api/v1/projects",
        params={"externalSystem": SYM["externalSystem"], "externalId": SYM["externalId"]},
        headers=auth(admin_key),
    )
    assert projects.status_code == 200, projects.text
    assert projects.json()["items"] == []
